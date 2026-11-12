package com.example.enterprise.contract;

import org.springframework.http.HttpStatus;

import java.util.Map;
import java.util.Set;

final class ContractStates {

    static final String DRAFT = "draft";
    static final String PENDING_REVIEW = "pending_review";
    static final String APPROVED = "approved";
    static final String REJECTED = "rejected";
    static final String EFFECTIVE = "effective";
    static final String EXPIRED = "expired";
    static final String TERMINATED = "terminated";

    private static final Map<String, Set<String>> NEXT = Map.of(
            DRAFT, Set.of(PENDING_REVIEW, TERMINATED),
            PENDING_REVIEW, Set.of(APPROVED, REJECTED),
            REJECTED, Set.of(DRAFT, TERMINATED),
            APPROVED, Set.of(EFFECTIVE, TERMINATED),
            EFFECTIVE, Set.of(EXPIRED, TERMINATED),
            EXPIRED, Set.of(TERMINATED),
            TERMINATED, Set.of());

    private static final Map<String, String> LABELS = Map.of(
            DRAFT, "草稿",
            PENDING_REVIEW, "待审核",
            APPROVED, "已审核",
            REJECTED, "已驳回",
            EFFECTIVE, "已生效",
            EXPIRED, "已到期",
            TERMINATED, "已终止");

    private ContractStates() {
    }

    static boolean known(String status) {
        return status != null && NEXT.containsKey(status);
    }

    static boolean canEdit(String status) {
        return DRAFT.equals(status) || REJECTED.equals(status);
    }

    static void requireTransition(String from, String to) {
        if (!known(from) || !known(to) || !NEXT.get(from).contains(to)) {
            throw new ContractException(HttpStatus.CONFLICT,
                    "不允许从" + LABELS.getOrDefault(from, from) + "变更为" + LABELS.getOrDefault(to, to));
        }
    }
}
