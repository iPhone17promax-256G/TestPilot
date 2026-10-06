FROM node:22-slim

ARG APPIUM_VERSION=3.8.0
ARG UIAUTOMATOR2_VERSION=8.7.0
ARG ANDROID_TOOLS_VERSION=16111833
ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    APPIUM_HOME=/opt/appium
ENV PATH=${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/emulator:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless curl unzip ca-certificates util-linux libgl1 libpulse0 libnss3 \
    libx11-6 libxcb1 libxkbcommon0 libxcb-cursor0 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
    && curl -4 --http1.1 -fsSL --connect-timeout 30 --max-time 600 --retry 5 --retry-all-errors https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_TOOLS_VERSION}_latest.zip -o /tmp/android-tools.zip \
    && unzip -q /tmp/android-tools.zip -d ${ANDROID_HOME}/cmdline-tools \
    && mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest \
    && rm /tmp/android-tools.zip \
    && yes | sdkmanager --licenses >/dev/null
RUN sdkmanager 'platform-tools' 'platforms;android-30' 'build-tools;30.0.3'
RUN sdkmanager 'emulator'
RUN sdkmanager 'system-images;android-30;default;x86_64'
RUN npm install -g appium@${APPIUM_VERSION} \
    && appium driver install uiautomator2@${UIAUTOMATOR2_VERSION} \
    && npm cache clean --force
RUN curl -4 --http1.1 -fsSL --connect-timeout 30 --max-time 600 --retry 5 --retry-all-errors \
    https://github.com/appium/android-apidemos/releases/download/v6.0.18/ApiDemos-debug.apk -o /opt/ApiDemos.apk

RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
COPY deploy/appium/start.sh /opt/start-appium.sh
COPY deploy/appium/device-node.py /opt/device-node.py
RUN sed -i 's/\r$//' /opt/start-appium.sh && chmod +x /opt/start-appium.sh
EXPOSE 4723
ENTRYPOINT ["/opt/start-appium.sh"]
