Changes in model.py:
Modified _init_ to load both a capable reasoning model and a fast, cheap model instead of only 1 model

Modified generate_json to tell text.py which model to use

Created a task_complexity routing flag in generate_json.

This  prevents burning expensive tokens on trivial stages like project initialization or basic JSON formatting.



Changes in proposals.py:
The Change: Threaded the task_complexity flag through to generate_json and added a hook to read repair_memory.txt during failed attempts.

Why: To ensure the cheap model is actually invoked when requested by the orchestrator, and to give the agent historical context on why previous tests failed so it doesn't waste loops.


Changes in tools.py:

The Change: Implemented forced truncation in read_file. It now limits unconstrained reads to 100 lines and hard-caps requested chunks at 250 lines, appending a warning message to the AI.

Why: To drastically reduce token consumption on large files and force the model to focus on specific code blocks, improving editing accuracy.

