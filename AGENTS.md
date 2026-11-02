# Instruções permanentes

- Preserve regras de negócio, contratos e a separação entre auditoria read-only e escrita supervisionada.
- Nunca exponha, altere ou versione credenciais, webhooks, `.env`, estados, logs ou relatórios operacionais.
- Não execute migrações reais nem habilite escritas sem autorização específica.
- Prefira alterações mínimas e execute testes proporcionais ao risco, sem repetir validações sem necessidade.
- Use contexto e tokens com economia; entregue relatórios objetivos e baseados em evidências.
- Não crie commits, reescreva histórico, configure remotos ou faça push sem aprovação.
