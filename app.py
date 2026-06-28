"""
JobHawk Web — Vaughn Krogman Job Search Dashboard
Run with: python app.py
Then open: http://localhost:5000
"""

import csv
import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, send_file
from apscheduler.schedulers.background import BackgroundScheduler
