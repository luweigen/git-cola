#!/bin/sh
#for windows
#conda create -n git-cola python=3.12 -y
#conda activate git-cola
#git clone git@github.com:luweigen/git-cola.git
#pip install --editable '.[extras,pyqt6]'
GIT_EDITOR="'C:/Program Files/Notepad++/notepad++.exe' -multiInst -notabbar -nosession -noPlugin" /c/conda_envs/git-cola/python -m cola dag --all --orphan-isolate