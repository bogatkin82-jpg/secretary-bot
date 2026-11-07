FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# ffmpeg — для расшифровки голосовых и кружков; curl — для сертификатов.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Сертификаты НУЦ Минцифры — нужны для GigaChat (Сбер).
# Скачиваем с проверкой SHA256: подмена файла на стороне источника не пройдёт.
# При ротации сертификатов обновите URL и хеши ниже.
RUN mkdir -p certs \
    && curl -fsSL --retry 3 --max-time 30 -o certs/russian_trusted_root_ca.crt \
        https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
    && echo "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504  certs/russian_trusted_root_ca.crt" \
        | sha256sum -c - \
    && curl -fsSL --retry 3 --max-time 30 -o certs/russian_trusted_sub_ca.crt \
        https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
    && echo "f0ae589f36774f29ef3648f7984b08d42fcce6f1ffeeb6236d773daeb2744ea6  certs/russian_trusted_sub_ca.crt" \
        | sha256sum -c -

# Бот не работает от root. UID фиксирован, чтобы права на ./data были предсказуемы.
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/bot --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app

COPY --chown=bot:bot bot ./bot
USER bot
CMD ["python", "-m", "bot"]
