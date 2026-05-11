This is the module's building instruction for AI agent.

Each module is separate with others, so we should not depend on functions out of this module.We can use external libraries, but not code from other modules in this codebase in side app folder. If we find some interesting code from other part of this codebase, do not use their function, direct copy and paste it, or just rewrite it in this module.

The main purpose of this module is to sync our DB's instagram_posts table, and we need to make updates since our DB has been changed.

please read app/event_deduplication_module/contract/contract.md and app/event_deduplication_module/contract/db_contract.md