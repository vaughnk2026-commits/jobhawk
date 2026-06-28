@echo off
echo.
echo  Installing dependencies...
pip install -r requirements.txt
echo.
echo  Starting JobHawk Web...
echo  Open http://localhost:5000 in your browser
echo.
python app.py
pause
