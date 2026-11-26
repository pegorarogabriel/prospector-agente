-- Mensagens que o bot mandou e até onde as respostas do Telegram já foram lidas.
-- Ver docs/arquitetura.md, seção 8.

-- Cada mensagem amarra o post à pergunta feita. Quando eu toco num botão, o
-- Telegram devolve só o chat e a mensagem: é por aqui que se chega ao post.
CREATE TABLE notifications (
    chat_id         bigint NOT NULL,
    message_id      bigint NOT NULL,
    post_id         text NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    source          text NOT NULL CHECK (source IN ('notification', 'audit')),
    sent_at         timestamptz NOT NULL,
    -- Os motivos de ✗ oferecidos nesta mensagem, na ordem dos botões (w0, w1, w2).
    reject_reasons  jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX notifications_post_id_idx ON notifications (post_id);

-- Estado pequeno das rotinas, chave e valor. Hoje só o offset do getUpdates.
CREATE TABLE app_state (
    key    text PRIMARY KEY,
    value  text NOT NULL
);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_state ENABLE ROW LEVEL SECURITY;
