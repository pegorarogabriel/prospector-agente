-- Regras de triagem em linguagem natural: o que o juiz lê antes de cada post.
-- Ver docs/arquitetura.md, seção 5. É esta tabela que o agente semanal ajusta.
--
-- As regras iniciais foram escritas a partir dos motivos reais das minhas
-- 124 respostas no FacebookProspector.

CREATE TABLE triage_rules (
    id          text PRIMARY KEY,
    text        text NOT NULL CHECK (btrim(text) <> ''),
    active      boolean NOT NULL DEFAULT true,
    created_by  text NOT NULL CHECK (created_by IN ('seed', 'agent', 'manual')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE triage_rules ENABLE ROW LEVEL SECURITY;

INSERT INTO triage_rules (id, text, created_by) VALUES
    ('seed-01-oferta',
     'Aprove só oferta de empresa, transportadora ou embarcador que busca agregado, '
     'ou que tem carga para truck ou toco, de preferência com baú.',
     'seed'),
    ('seed-02-motorista',
     'Reprove motorista ou autônomo se oferecendo: "tenho truck disponível", '
     '"procuro carga", "estou vazio", "quero trabalho". O interesse é em quem tem carga, '
     'não em outro motorista procurando serviço.',
     'seed'),
    ('seed-03-servico',
     'Reprove quem vende, aluga ou financia veículo, oferece curso, oficina, proteção '
     'veicular, seguro, rastreador, documentação, averbação, emissão de nota ou cotação '
     'de frete. Isso é vender serviço para o caminhoneiro, não oferecer agregamento.',
     'seed'),
    ('seed-04-veiculo',
     'Reprove se a oferta é só para veículos fora da operação (carreta, bitrem, '
     'rodotrem, bitruck, VUC, 3/4, van, fiorino, utilitário) ou carroceria fora (sider, '
     'graneleiro, caçamba, prancha, tanque, frigorífico). Se a mesma oferta também '
     'aceita truck ou toco baú, pode aprovar.',
     'seed'),
    ('seed-05-regiao',
     'Reprove rota que sai do estado de São Paulo ou fica longe de Guarulhos '
     '(mais de uns 100 km). Sem cidade no post, não reprove por região.',
     'seed'),
    ('seed-06-assunto',
     'Reprove post que não é sobre transporte de carga: post pessoal, propaganda de '
     'outra coisa, notícia, meme.',
     'seed'),
    ('seed-07-grupo',
     'Convite para grupo de WhatsApp de cargas ou agregados na região pode ser aprovado, '
     'mas com nota menor que uma oferta direta.',
     'seed');
