Currently, I am trying to design a new AI-native way of programming. I call it Contract-Oriented-Programming.

In the AI era, we are using AI agents to do many programming tasks, but I have had a very hard time dealing with them and have met many problems.

(When I'm using AI agent to vibe code programs at the beginning, the speed is very fast, but at a certain point, each time when I add a new feature or upgrade the old system, there will be a lot of weird bugs, You can fix it manually, but when you want AI to add a new feature or do some change on the old code, there will always be new bug. And at a certain point, fixing bug takes more time than development, and this is a disaster.)

1, AI-generated code will usually use functions that does not belong to this module, and we don't really understand AI-generated code, we don't have control of it, so it will cause chaos.

2, AI-generated code is extremely heavy and uses too many dependencies.

3, AI-generated code heavily depends on which AI agent you use, which model you use, and what context, history, and memory the AI has. You do not really know what files the AI is reading or what hidden context is affecting the result. These things exist, but you have no control over them. That is not acceptable.

4, we do not really have proper version control for AI-generated logic. There is a lot of ghost code in AI-generated projects. This kind of code looks correct, but actually has no purpose. The logic is duplicated, redundant, and unnecessary, but it still exists. And when you continue modifying the system, even more ghost code appears.

In traditional programming, even ugly code usually has some purpose and logic behind it. But AI-generated code is different. The code itself can no longer serve as reliable version control or logical history. We need a separate file to explicitly describe the logic and reasoning of the system.



To solve these problems, I decided to design a new coding standard called Contract-Oriented Programming.

What we are doing is enforcing the input data: what data we are getting, where it comes from, and what our output data should be. We define what the final result should look like and how many outputs we should have.

We also restrict the dependencies we use. We restrict what files the AI is allowed to read. If we want to use code or functions from another part of the application, we can only use shared functions inside that folder or contracted functions. We cannot directly use functions from other modules.

The overall idea is to make AI work like a compiler. We use human language, mainly English, to write a contract, and the AI compiles this contract into a Python file. Then the Python runtime executes it and eventually runs it as machine code on our computer.

The relationship is similar to this:
The contract is like a C language file, the AI is like the GCC compiler, and Python is like the machine code runtime.

I want to add restrictions to make AI-generated code more controllable. There will always be some uncertainty, but we do not need 0% uncertainty in every part of the code. I am trying to reduce the uncertainty enough to make the system run safely and reliably.

Basically, what we are doing is trading some development speed and extra effort for better certainty, better control, and better scalability of the codebase.

In the end, the contracts are the final outcome, and everything should be described inside the contracts. The contract is the system itself. Nothing should exist outside the contract while still being part of the system.