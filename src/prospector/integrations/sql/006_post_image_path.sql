-- Onde está a cópia da imagem de cada post no Supabase Storage (bucket privado
-- post-images). A URL de imagem do Facebook expira em poucos dias; sem a cópia,
-- o backtest e o agente semanal não conseguem rever posts só de imagem.
-- Ver docs/arquitetura.md, seção 5.

ALTER TABLE posts ADD COLUMN image_path text;
