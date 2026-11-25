# Como contribuir

Contribuições podem melhorar validações, exemplos fictícios e documentação de decisões.

## Antes de enviar

1. Use Python 3.10 ou superior e apenas a biblioteca padrão.
2. Rode `python -m unittest discover -s tests -v` e os dois exemplos da CLI.
3. Explique a mudança e suas premissas. Novos testes devem conferir comportamento observável, especialmente precisão e rejeição de entradas incompatíveis.
4. Mantenha exemplos sem nomes de empresas, pessoas, assinaturas, recursos internos ou credenciais.

## Atualizar preços manualmente

- Consulte somente a Azure Retail Prices API pública, sem autenticação.
- Leia todas as páginas retornadas antes de escolher um preço.
- Filtre região `brazilsouth`, moeda `USD`, `Consumption` e unidade `1 Hour`.
- Exclua Windows, Spot, Low Priority, Reservation e DevTestConsumption. Confira produto, SKU, medidor, camada mínima e região de medidor principal.
- Preserve os campos de origem do item e a URL exata da consulta. Não confunda `effectiveStartDate` com a data da consulta.
- Registre `consulted_at` com a data UTC real e HTTP 200 apenas se isso foi observado. Uma indisponibilidade da fonte não autoriza atualizar a data de um preço antigo como se tivesse sido consultado novamente.
- Se usar dados inventados, identifique `kind` como `simulation` e `source.http_status` como `null`. Deixe claro no material que não são preços públicos verificados.
- Atualize os exemplos e as expectativas que dependem das tarifas de maneira explícita. Preserve a reprodutibilidade do snapshot anterior no histórico.

Não adicione consulta automática à execução da CLI nem autenticação, assinatura Azure ou implantação. Mudanças no escopo de moedas e unidades precisam de uma proposta específica, não de conversão implícita.

## Evidência e direitos

Economia calculada é potencial, sob as premissas do cenário. Para alegar economia realizada seria necessária medição externa, com método documentado. Não publique dados financeiros internos para demonstrar o projeto.

Textos e código enviados devem ser autorais ou permitir a contribuição sob MIT. Fontes externas devem ser citadas e continuar sob os direitos e termos de seus titulares.
