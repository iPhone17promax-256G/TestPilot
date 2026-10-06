FROM ubuntu:22.04
ARG JMETER_VERSION=5.6.3
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jre-headless curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -4 --http1.1 -fsSL --connect-timeout 20 --max-time 300 --retry 3 https://downloads.apache.org/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz -o /tmp/jmeter.tgz && curl -4 --http1.1 -fsSL --connect-timeout 20 --max-time 300 --retry 3 https://downloads.apache.org/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz.sha512 -o /tmp/jmeter.sha512 && cd /tmp && printf '%s  jmeter.tgz\n' "$(head -c 128 jmeter.sha512)" | sha512sum -c - && tar -xzf jmeter.tgz -C /opt && mv /opt/apache-jmeter-${JMETER_VERSION} /opt/jmeter && rm /tmp/jmeter*
ENV HEAP="-Xms128m -Xmx512m -XX:MaxMetaspaceSize=128m"
ENTRYPOINT ["/opt/jmeter/bin/jmeter"]
