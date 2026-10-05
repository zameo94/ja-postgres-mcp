-- Demo database for manual testing of ja-postgres-mcp.
--
-- The postgres image runs every *.sql in /docker-entrypoint-initdb.d on the
-- first initialization (empty data directory), so this seed runs only when the
-- demo database is created for the first time.
--
-- Everything lives in the `demo` schema so it never collides with application
-- data. The MCP tools do not know this schema: it is only a fixture to try
-- discovery and read-only queries on a realistic structure.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;
SET search_path TO demo;

CREATE TABLE customers (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    email      text NOT NULL UNIQUE,
    country    text NOT NULL,
    created_at date NOT NULL
);

CREATE TABLE products (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    category   text NOT NULL,
    unit_price numeric(10, 2) NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE orders (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (id),
    status      text NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled')),
    ordered_at  date NOT NULL
);

CREATE TABLE order_items (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id   integer NOT NULL REFERENCES orders (id),
    product_id integer NOT NULL REFERENCES products (id),
    quantity   integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(10, 2) NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE payments (
    id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders (id),
    amount   numeric(10, 2) NOT NULL CHECK (amount >= 0),
    paid_at  date,
    method   text NOT NULL
);

CREATE INDEX orders_customer_id_idx ON orders (customer_id);
CREATE INDEX orders_ordered_at_idx ON orders (ordered_at);
CREATE INDEX order_items_order_id_idx ON order_items (order_id);
CREATE INDEX payments_order_id_idx ON payments (order_id);

CREATE VIEW monthly_revenue AS
SELECT date_trunc('month', paid_at)::date AS month,
       sum(amount) AS revenue
FROM payments
WHERE paid_at IS NOT NULL
GROUP BY 1;

-- Deterministic fake data (reproducible, no randomness).

INSERT INTO customers (name, email, country, created_at)
SELECT 'Customer ' || i,
       'customer' || i || '@example.com',
       (ARRAY['IT', 'US', 'DE', 'FR'])[1 + (i % 4)],
       DATE '2024-01-01' + (i % 365)
FROM generate_series(1, 50) AS i;

INSERT INTO products (name, category, unit_price)
SELECT 'Product ' || i,
       (ARRAY['hardware', 'software', 'service'])[1 + (i % 3)],
       (10 + (i % 7) * 5)::numeric(10, 2)
FROM generate_series(1, 20) AS i;

INSERT INTO orders (customer_id, status, ordered_at)
SELECT 1 + (i % 50),
       CASE
           WHEN i % 10 = 0 THEN 'cancelled'
           WHEN i % 3 = 0 THEN 'pending'
           ELSE 'paid'
       END,
       DATE '2025-01-01' + (i % 365)
FROM generate_series(1, 200) AS i;

-- Three line items per order, derived from the orders themselves.
--
-- The product and the quantity must vary *inside* an order, otherwise the
-- schema's central table carries no information. That is achieved by deriving
-- both from the order id with coprime multipliers instead of iterating a
-- single series: 3 is coprime with the 20 products, so different orders pick
-- different products, and the consecutive offsets give each order three
-- distinct products and three distinct quantities.
--
-- unit_price is copied from products, so the line price always matches the
-- catalogue price.
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.id,
       p.id,
       1 + ((o.id + s.i) % 5),
       p.unit_price
FROM orders AS o
CROSS JOIN generate_series(0, 2) AS s(i)
JOIN products AS p ON p.id = 1 + ((o.id * 3 + s.i) % 20);

-- One payment per paid order, for exactly the order total, on the order date.
-- Pending and cancelled orders have no payment row at all.
INSERT INTO payments (order_id, amount, paid_at, method)
SELECT o.id,
       sum(oi.quantity * oi.unit_price),
       o.ordered_at,
       (ARRAY['card', 'cash', 'transfer'])[1 + (o.id % 3)]
FROM orders AS o
JOIN order_items AS oi ON oi.order_id = o.id
WHERE o.status = 'paid'
GROUP BY o.id;

COMMIT;
