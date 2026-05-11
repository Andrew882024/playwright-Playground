This application is a continuous script-driven event processing system.  
All processed data is stored in the database and passed between modules through persistent storage.

The system is currently organized as a sequential pipeline of independent modules.

Current pipeline flow:

1. Instagram Scraper Module
2. AI Analyze Service Module
3. event_deduplication_module
4. Add Our Own S3 URL Module
5. Fomo Sync Module (Triggered by API call)