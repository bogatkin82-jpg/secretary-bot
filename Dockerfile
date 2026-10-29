FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# ffmpeg — для расшифровки голосовых и кружков
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Сертификаты НУЦ Минцифры — нужны для GigaChat (Сбер)
ADD https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt certs/russian_trusted_root_ca.crt
ADD https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt certs/russian_trusted_sub_ca.crt
COPY bot ./bot
CMD ["python", "-m", "bot"]
