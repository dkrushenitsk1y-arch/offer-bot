@echo off
title Freight Offer Bot

echo Starting Freight Offer Bot...
echo.

cd /d D:\PROJECTS\offer-bot

echo Activating virtual environment...
call .\.venv\Scripts\activate.bat

echo Starting server...
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pause