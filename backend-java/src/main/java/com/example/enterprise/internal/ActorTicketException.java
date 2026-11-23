package com.example.enterprise.internal;

import com.example.enterprise.integration.agent.AgentServiceException;
import org.springframework.http.HttpStatus;

/**
 * Redis could not issue, consume, or revoke an actor ticket. The message is
 * fixed so logs and HTTP responses never include the ticket, key, or value.
 */
public class ActorTicketException extends AgentServiceException {

    public ActorTicketException() {
        super(HttpStatus.SERVICE_UNAVAILABLE, "Ticket 存储不可用");
    }
}
