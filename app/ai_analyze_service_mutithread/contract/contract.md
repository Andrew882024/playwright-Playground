This is the module's building instruction for AI agent.

Each module is separate with others, so we should not depend on functions out of this module.We can use external libraries, but not code from other modules in this codebase in side app folder. If we find some interesting code from other part of this codebase, do not use their function, direct copy and paste it, or just rewrite it in this module.

please take look at all files in ai_analyze_service folder

the service in the floder is already working, I want to make it faster with mutithreading. I want to use redis as a message queue.

the service first go to db to get all data that we need to proccess.

I want 4 threads, which each thread is related with one of these model:
"gemini-3-flash-preview",
"gemini-3.1-flash-lite-preview",
"gemini-2.5-flash",
"gemini-2.5-flash-lite",

the redis message queue assign works that each thread need to do.

we default run 4 workers

4 workers will work at same time, each one will have tasks, not all on one worker.

when one of them reach the limit and steal fail after retry, the message queue reassign the task

important, when a post is prossessed it will be update in the db immediately, and after the related redis queue message will be consumed.

we need to change our docker-compose.yml to add a redis, so we can use it