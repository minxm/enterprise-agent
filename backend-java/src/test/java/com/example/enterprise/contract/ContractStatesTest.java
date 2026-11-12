package com.example.enterprise.contract;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ContractStatesTest {

    @Test
    void legalTransitionsAndEditableStates() {
        assertDoesNotThrow(() -> ContractStates.requireTransition("draft", "pending_review"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("pending_review", "approved"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("pending_review", "rejected"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("rejected", "draft"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("approved", "effective"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("effective", "expired"));
        assertDoesNotThrow(() -> ContractStates.requireTransition("expired", "terminated"));
        assertTrue(ContractStates.canEdit("draft"));
        assertTrue(ContractStates.canEdit("rejected"));
        assertFalse(ContractStates.canEdit("pending_review"));
        assertFalse(ContractStates.canEdit("effective"));
        assertFalse(ContractStates.canEdit("terminated"));
    }

    @Test
    void illegalTransitionIsRejected() {
        ContractException exception = assertThrows(ContractException.class,
                () -> ContractStates.requireTransition("draft", "effective"));
        assertEquals("不允许从草稿变更为已生效", exception.getMessage());
        assertThrows(ContractException.class, () -> ContractStates.requireTransition("terminated", "draft"));
        assertThrows(ContractException.class, () -> ContractStates.requireTransition("approved", "draft"));
    }
}
