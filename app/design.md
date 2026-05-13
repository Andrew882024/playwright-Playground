Currently we are connecting our module by running scripts in specific orider.

It works well functionally, but it is running to slow, mainly because of 2 reasons, 
    1, the pieline is running synchronously and blokingly between modules.
    2, we spent a lot of time waiting gemini ai api's reponse, due to we are running synchronously and blokingly in inside modules(currently ai_analyze_service is the main bottle neck)

    //about problem 1 we can use message queue.

To solve those problem:
    I want to solve the problem 2 since it is the main bottle neck. I plan to use mutithreading and redis message queue to sole this problem.