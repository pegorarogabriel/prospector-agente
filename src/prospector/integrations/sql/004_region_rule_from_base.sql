-- A regra de região do juiz passa a refletir a limitação real da operação:
-- o caminhão sai de Guarulhos e volta para Guarulhos (docs/arquitetura.md,
-- seção 1). A versão da 003 falava em "longe de Guarulhos (mais de uns 100 km)",
-- o que deixava passar operação baseada em Sorocaba ou Sumaré.
--
-- Só altera a regra se ela ainda for a semente original: se o agente semanal
-- ou eu já a tivermos mudado, esta migração não passa por cima.

UPDATE triage_rules
SET text = 'A origem da operação (onde o caminhão carrega ou se apresenta) precisa ficar '
           'a até uns 30 km de Guarulhos: Guarulhos, Arujá, Itaquaquecetuba, Poá, '
           'Mairiporã, zonas Norte e Leste de São Paulo. Reprove operação baseada em outra '
           'cidade, mesmo perto (Sorocaba, Campinas, Sumaré, Jundiaí, Mogi das Cruzes): '
           'não compensa deslocar o caminhão. O destino pode ser longe, desde que dentro '
           'do estado de São Paulo; fora de SP, reprove. Sem cidade no post, não reprove '
           'por região.'
WHERE id = 'seed-05-regiao'
  AND created_by = 'seed'
  AND text LIKE 'Reprove rota que sai do estado de São Paulo ou fica longe de Guarulhos%';
