-- O agente semanal (docs/arquitetura.md, seção 6).
--
-- O estado do grafo LangGraph (checkpoints) não mora aqui: as tabelas dele são
-- criadas pelo próprio PostgresSaver.setup(), e o comando `weekly` liga o RLS
-- nelas logo depois.

-- Uma linha por domingo. week_id é também o thread_id do grafo.
CREATE TABLE weekly_runs (
    week_id             text PRIMARY KEY,
    started_at          timestamptz NOT NULL,
    resume_after        timestamptz NOT NULL,
    status              text NOT NULL CHECK (status IN ('waiting', 'done')),
    finished_at         timestamptz,
    -- A mensagem do resumo: é por ela que o botão Desfazer chega à semana.
    summary_chat_id     bigint,
    summary_message_id  bigint,
    undone_at           timestamptz
);

-- Tudo o que o agente aplicou, com o motivo. O botão Desfazer reverte a semana.
CREATE TABLE changelog (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    week_id      text NOT NULL REFERENCES weekly_runs (week_id),
    kind         text NOT NULL CHECK (kind IN ('add_rule', 'deactivate_rule', 'add_recipe')),
    target_id    text NOT NULL,
    reason       text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    applied_at   timestamptz NOT NULL,
    reverted_at  timestamptz
);

CREATE INDEX changelog_week_id_idx ON changelog (week_id);

-- Que posts já entraram no placar. Cada post conta uma vez só, na semana em
-- que o resultado dele ficou definido (o aprovado espera até 7 dias pela
-- minha resposta). Uma semana congelada marca os seus como 'frozen'.
CREATE TABLE scored_posts (
    post_id    text PRIMARY KEY REFERENCES posts (id) ON DELETE CASCADE,
    week_id    text NOT NULL,
    outcome    text NOT NULL,
    scored_at  timestamptz NOT NULL DEFAULT now()
);

-- O placar começa a contar a partir daqui. O que já estava no banco (o
-- histórico do FacebookProspector) já entrou no prior das receitas na
-- importação; contar de novo seria contar duas vezes.
INSERT INTO scored_posts (post_id, week_id, outcome)
SELECT id, 'before-agent', 'before_agent' FROM posts;

-- Busca de teste do agente (testar_receita): o termo ainda não é receita, mas
-- o gasto precisa entrar na conta do mês.
ALTER TABLE search_runs ALTER COLUMN recipe_id DROP NOT NULL;

ALTER TABLE weekly_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE changelog ENABLE ROW LEVEL SECURITY;
ALTER TABLE scored_posts ENABLE ROW LEVEL SECURITY;
