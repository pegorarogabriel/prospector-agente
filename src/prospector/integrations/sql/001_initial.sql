-- Schema inicial: receitas, posts, decisões da triagem e minhas respostas.
-- Ver docs/arquitetura.md, seções 3, 4 e 11.
--
-- Sem campos de autor: não são necessários para aprender.
-- Tabelas sem nome de schema: o migrador aplica no search_path da conexão,
-- o que deixa os testes rodarem num schema temporário isolado.

CREATE TABLE recipes (
    id          text PRIMARY KEY,
    query       text NOT NULL,
    params      jsonb NOT NULL DEFAULT '{}',
    -- Observações, já com esquecimento aplicado; o prior Beta(1, 1) é somado
    -- no código (Recipe.alpha / Recipe.beta), nunca guardado.
    successes   double precision NOT NULL DEFAULT 0 CHECK (successes >= 0),
    failures    double precision NOT NULL DEFAULT 0 CHECK (failures >= 0),
    status      text NOT NULL CHECK (status IN ('active', 'candidate', 'retired')),
    origin      text NOT NULL CHECK (origin IN ('legacy', 'agent', 'manual')),
    parent_id   text REFERENCES recipes (id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE posts (
    id            text PRIMARY KEY,
    -- Vazio quando o post é só imagem; nunca os dois vazios.
    text          text NOT NULL,
    image_url     text,
    url           text NOT NULL,
    posted_at     timestamptz NOT NULL,
    location      text,
    collected_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(text) <> '' OR image_url IS NOT NULL)
);

CREATE INDEX posts_posted_at_idx ON posts (posted_at);

-- Um post encontrado por mais de uma receita conta para todas (seção 3).
CREATE TABLE post_recipes (
    post_id    text NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    recipe_id  text NOT NULL REFERENCES recipes (id),
    PRIMARY KEY (post_id, recipe_id)
);

CREATE TABLE triage_results (
    post_id      text PRIMARY KEY REFERENCES posts (id) ON DELETE CASCADE,
    approved     boolean NOT NULL,
    score        double precision NOT NULL CHECK (score BETWEEN 0 AND 1),
    reason       text NOT NULL,
    vehicle      text,
    origin       text,
    destination  text,
    decided_at   timestamptz NOT NULL DEFAULT now()
);

-- Uma resposta por post e por pergunta (notificação ou auditoria).
CREATE TABLE feedback (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id      text NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    verdict      text NOT NULL CHECK (verdict IN ('right', 'wrong')),
    source       text NOT NULL CHECK (source IN ('notification', 'audit')),
    answered_at  timestamptz NOT NULL,
    reason       text,
    UNIQUE (post_id, source)
);

-- "A semana teve feedback?" sai desta coluna (seção 4).
CREATE INDEX feedback_answered_at_idx ON feedback (answered_at);

-- O Supabase expõe toda tabela do schema public pela API REST. Com RLS ligado
-- e nenhuma policy, a API não lê nada; a conexão direta do sistema, dona das
-- tabelas, não é afetada. Os posts têm nome e telefone de gente real.
ALTER TABLE recipes ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_recipes ENABLE ROW LEVEL SECURITY;
ALTER TABLE triage_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
