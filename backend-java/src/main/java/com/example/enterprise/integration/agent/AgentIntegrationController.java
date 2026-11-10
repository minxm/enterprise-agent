package com.example.enterprise.integration.agent;

import com.example.enterprise.common.response.ApiResponse;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 浏览器通过 Java 查看 Python Agent 是否可用。
 * 本接口返回成功表示依赖健康；Java 进程存活但 Python 不可用时返回依赖错误。
 */
@RestController
@RequestMapping("/api/v1/integration/agent")
public class AgentIntegrationController {

    private final AgentClient agentClient;

    public AgentIntegrationController(AgentClient agentClient) {
        this.agentClient = agentClient;
    }

    @GetMapping("/health")
    public ApiResponse<AgentHealthResponse> health() {
        return ApiResponse.success(agentClient.health());
    }
}
