-- Cada chamada ao Apify: quanto foi pedido e quanto foi cobrado.
-- É a fonte do "gasto do mês" que a trava de orçamento consulta antes de toda
-- busca (docs/arquitetura.md, seção 7).

CREATE TABLE search_runs (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ran_at     timestamptz NOT NULL,
    recipe_id  text NOT NULL REFERENCES recipes (id),
    requested  integer NOT NULL CHECK (requested >= 0),
    charged    integer NOT NULL CHECK (charged >= 0),
    live       boolean NOT NULL
);

CREATE INDEX search_runs_ran_at_idx ON search_runs (ran_at);

ALTER TABLE search_runs ENABLE ROW LEVEL SECURITY;
