FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py tools.py config.json prompt.txt ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--client.toolbarMode=viewer", "--theme.base=light", "--theme.primaryColor=#185ABD", "--browser.gatherUsageStats=false"]
