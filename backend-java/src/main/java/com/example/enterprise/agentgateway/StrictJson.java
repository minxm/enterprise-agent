package com.example.enterprise.agentgateway;

import com.example.enterprise.identity.IdentityException;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validator;
import java.util.Set;
import org.springframework.http.HttpStatus;

final class StrictJson {

    private static final ObjectMapper MAPPER = JsonMapper.builder()
            .addModule(new JavaTimeModule())
            .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .build();

    private StrictJson() {
    }

    static <T> T read(JsonNode node, Class<T> type, Validator validator) {
        if (node == null || !node.isObject()) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        try {
            T value = MAPPER.treeToValue(node, type);
            if (value == null) {
                throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
            }
            Set<ConstraintViolation<T>> violations = validator.validate(value);
            if (!violations.isEmpty()) {
                throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
            }
            return value;
        } catch (JsonProcessingException exception) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
    }
}
