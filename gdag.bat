@echo off
set "GIT_EDITOR='C:/Program Files/Notepad++/notepad++.exe' -multiInst -notabbar -nosession -noPlugin"
start /b c:\conda_envs\git-cola\pythonw -m cola dag --all --orphan-isolate