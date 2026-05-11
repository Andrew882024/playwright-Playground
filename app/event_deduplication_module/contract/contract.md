This is the module's building instruction for AI agent.

Each module is separate with others, so we should not depend on functions out of this module.We can use external libraries, but not code from other modules in this codebase in side app folder. If we find some interesting code from other part of this codebase, do not use their function, direct copy and paste it, or just rewrite it in this module.

Before reading this markdown file, you should read db_contract.md.

This module can be run as a script.

input:
This module will get inputs from our database.
we will get id, profile_username, post_title, posted_unix_seconds, posted_time,
is_event, event_title, provider_name, post_description, location, 
duration_in_minutes, ai_analyzed, event_start_at, from instagram_posts table.

output: 
update is_duplicated in instagram_posts table

purpose:
There are some duplicate events in our database, I don't want to show duplicate events, so we add an is Duplicated column to check this. And if we find 4 posts that are duplicated, then we just left 1 of them is duplicate to false and others is duplicate to be true.

Implementation instruction:
We first get information from database. We want post information that it's is_event is true. And this same starting time appears at least twice. At the same time, at least one of them is_duplicate = null.

After this, we send information into Gemini AI. And we want to get the result of is any of them duplicate.If we get four duplicates events, we label one of them duplicate equals false and others duplicate equals true.