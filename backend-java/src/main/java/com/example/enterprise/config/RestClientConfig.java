package com.example.enterprise.config;

import java.time.Duration;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

@Configuration
@EnableConfigurationProperties(AgentServiceProperties.class)
public class RestClientConfig {

    @Bean
    RestClient agentRestClient(AgentServiceProperties properties) {
        return create(properties.getBaseUrl(), properties.getConnectTimeout(), properties.getReadTimeout());
    }

    /**
     * HttpURLConnection 的读超时作用在套接字读取上，响应头或响应体停住都会按时失败。
     */
    public static RestClient create(String baseUrl, Duration connectTimeout, Duration readTimeout) {
        SimpleClientHttpRequestFactory requestFactory = new SimpleClientHttpRequestFactory();
        requestFactory.setConnectTimeout(connectTimeout);
        requestFactory.setReadTimeout(readTimeout);
        return RestClient.builder()
                .baseUrl(trimTrailingSlash(baseUrl))
                .requestFactory(requestFactory)
                .build();
    }

    private static String trimTrailingSlash(String baseUrl) {
        if (baseUrl.endsWith("/")) {
            return baseUrl.substring(0, baseUrl.length() - 1);
        }
        return baseUrl;
    }
}
