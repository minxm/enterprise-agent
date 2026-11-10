package com.example.enterprise.common.request;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.common.response.GlobalExceptionHandler;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(ValidationProbeController.class)
@Import({RequestIdFilter.class, GlobalExceptionHandler.class})
class RequestIdFilterTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void generatesAnIdWhenTheHeaderIsMissing() throws Exception {
        String requestId = mockMvc.perform(get("/api/v1/test/ping"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.requestId").exists())
                .andReturn()
                .getResponse()
                .getHeader(RequestIds.HEADER);

        assertThat(requestId).matches("[A-Za-z0-9_-]{1,128}");
        assertThat(MDC.get(RequestIds.MDC_KEY)).isNull();
    }

    @Test
    void keepsAValidIncomingIdAndClearsMdc() throws Exception {
        mockMvc.perform(get("/api/v1/test/ping").header(RequestIds.HEADER, "trace_OK-1"))
                .andExpect(status().isOk())
                .andExpect(header().string(RequestIds.HEADER, "trace_OK-1"))
                .andExpect(jsonPath("$.requestId").value("trace_OK-1"));

        assertThat(MDC.get(RequestIds.MDC_KEY)).isNull();
    }

    @Test
    void replacesAnUnsafeId() throws Exception {
        String requestId = mockMvc.perform(get("/api/v1/test/ping").header(RequestIds.HEADER, "bad id\n<script>"))
                .andReturn()
                .getResponse()
                .getHeader(RequestIds.HEADER);

        assertThat(requestId).isNotEqualTo("bad id\n<script>");
        assertThat(requestId).matches("[A-Za-z0-9_-]{1,128}");
        assertThat(MDC.get(RequestIds.MDC_KEY)).isNull();
    }

    @Test
    void validationErrorCarriesRequestIdWithoutStackDetails() throws Exception {
        mockMvc.perform(post("/api/v1/test/validation")
                        .header(RequestIds.HEADER, "trace-valid")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{}"))
                .andExpect(status().isBadRequest())
                .andExpect(header().string(RequestIds.HEADER, "trace-valid"))
                .andExpect(jsonPath("$.code").value(400))
                .andExpect(jsonPath("$.message").value("请求参数不合法"))
                .andExpect(jsonPath("$.requestId").value("trace-valid"));

        String body = mockMvc.perform(post("/api/v1/test/validation")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\" "))
                .andReturn()
                .getResponse()
                .getContentAsString();
        assertThat(body).doesNotContain("Exception");
        assertThat(body).doesNotContain("java.");
        assertThat(MDC.get(RequestIds.MDC_KEY)).isNull();
    }

    }
