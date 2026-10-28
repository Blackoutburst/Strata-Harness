@echo off
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m strata_board_harness.local %*
