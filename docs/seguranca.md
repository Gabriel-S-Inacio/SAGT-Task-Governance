# Segurança e integridade

[Voltar ao README](../README.md)

## Limites de confiança

O SAGT opera entre um usuário local, arquivos locais e a API do Bitrix24. O webhook concede acesso conforme suas permissões e deve ser tratado como segredo. Relatórios e estados também são sensíveis porque podem conter nomes, IDs, títulos e vínculos de tarefas.

## Proteção de credenciais

- o webhook é lido da variável configurada, normalmente `BITRIX24_WEBHOOK_URL`;
- o `.env` local é carregado sem sobrescrever uma variável já presente no processo;
- a URL precisa usar HTTPS, não pode conter query string ou credenciais embutidas e deve corresponder ao host autorizado;
- localhost e endereços IP privados, reservados ou loopback são recusados como destino do webhook;
- mensagens de rede passam por redação do segredo antes de serem exibidas;
- credenciais não são gravadas em planos, modelos, relatórios ou históricos.

O repositório publica apenas `.env.example` com placeholder. A credencial real não deve ser copiada para issues, screenshots, logs ou exemplos.

## Separação entre leitura e escrita

O cliente de auditoria contém uma allowlist estritamente read-only. O cliente de migração é separado, restringe o método e valida os campos do patch. Essa divisão impede que um fluxo de auditoria invoque acidentalmente operações de escrita.

Escritas reais permanecem bloqueadas por padrão e dependem de liberação ambiental, aprovação do plano e ação explícita do operador.

## Integridade operacional

- validação de esquema, IDs, papéis e caminhos;
- paginação com verificação de cursores e total;
- checkpoints persistentes e artefatos vinculados ao identificador da execução;
- digest canônico dos planos de migração;
- invalidação de aprovações quando o plano muda;
- locks locais para estado e execução;
- pré-validação imediatamente antes da escrita;
- verificação do estado persistido após a escrita;
- ausência de retry automático para escritas ambíguas;
- neutralização de fórmulas em células exportadas.

## Servidor local

O Streamlit está configurado para `127.0.0.1:8501`. CORS e proteção XSRF permanecem habilitados, e a telemetria de uso está desativada. Essa configuração não transforma a aplicação em um serviço multiusuário ou pronto para exposição externa.

## Exclusões para publicação

O `.gitignore` exclui:

- `.env` e suas variações, preservando apenas `.env.example`;
- `config/*.toml`, preservando apenas `config/*.example.toml`;
- `.venv/`, caches Python e metadados de pacote;
- conteúdo de `.state/` e `output/`, preservando apenas `.gitkeep`;
- segredos locais do Streamlit;
- logs, NDJSON, planilhas, temporários e backups.

Arquivos ignorados continuam existindo localmente. A exclusão não remove nem sanitiza arquivos já presentes em um histórico Git anterior.

## Publicação responsável

Antes de tornar o repositório público:

1. confirme a autorização do titular do código;
2. defina uma licença de distribuição;
3. revise exatamente os arquivos que serão adicionados ao primeiro commit;
4. execute varredura de segredos no conteúdo e, se houver, em todo o histórico e tags;
5. valide que screenshots estejam anonimizados e autorizados;
6. trate qualquer credencial anteriormente publicada como potencialmente comprometida.

Sem um diretório `.git` disponível, o histórico e as tags não podem ser auditados. Nenhum histórico externo deve ser importado ou publicado antes dessa verificação.
