please take look at all files in ai_analyze_service folder

the service in the floder is already working, I want to make it faster with mutithreading. I want to use redis as a message queue.

the service first go to db to get all data that we need to proccess.

the redis message queue assign works that each thread need to do.

I want 4 threads, which each thread is related with one of these model:
"gemini-3-flash-preview",
"gemini-3.1-flash-lite-preview",
"gemini-2.5-flash",
"gemini-2.5-flash-lite",

when one of them reach the limit and steal fail after retry, the message queue reassign the task

important, when a post is prossessed it will be update in the db immediately, and after the related redis queue message will be consumed.

what you think about this design?


your thought: