# Aprendizados técnicos

[Voltar ao README](../README.md)

## Separar descoberta de alteração

Auditar e modificar tarefas possuem perfis de risco diferentes. Manter clientes REST separados, com allowlists independentes, tornou essa fronteira verificável no código e evitou que a coleta read-only herdasse capacidade de escrita.

## Paginação exige evidência de conclusão

O tamanho da página não comprova o fim de uma consulta. O fluxo passou a seguir `next` explicitamente e a rejeitar cursores repetidos, regressivos ou incompatíveis com `total`. Checkpoints preservam os registros já coletados quando a cobertura não pode ser comprovada.

## Aprovar a intenção não basta

Uma confirmação genérica pode continuar válida mesmo depois de uma mudança no plano. No SAGT, o digest cobre o conjunto efetivo de operações, e a aprovação persiste tarefas, patches e correções autorizadas. Alterar a prévia invalida a aprovação.

## Estado atual prevalece sobre a fotografia auditada

O relatório define o escopo, mas não garante que a tarefa permaneceu igual. A migração relê cada tarefa durante a pré-validação e antes da escrita. Divergências deixam de ser suposições silenciosas e passam a ser bloqueios observáveis.

## Escritas ambíguas não devem ser repetidas

Um timeout ou HTTP 5xx pode ocorrer depois que o servidor recebeu a alteração. Repetir automaticamente poderia duplicar ou sobrescrever efeitos. O cliente sinaliza a ambiguidade, relê a tarefa e, sem comprovação, registra resultado incerto para decisão humana.

## Listas precisam preservar o que está fora do escopo

Participantes e observadores são campos multivalorados. A substituição é calculada sobre o estado atual para conservar os demais membros. A verificação posterior compara o conjunto final e identifica efeitos colaterais.

## Persistência local também precisa de integridade

Estados gravados em arquivo podem sofrer concorrência e interrupções. Locks, escrita temporária com substituição atômica, separação entre checkpoints dry-run e reais e identificação por `run_id` tornam o fluxo retomável e reduzem associações incorretas entre execuções.

## Documentação pública deve descrever limites

O valor do sistema está na rastreabilidade e nos controles, não em promessas absolutas. A documentação registra capacidades, condições de uso e riscos residuais sem expor dados reais ou inventar métricas de produtividade.
