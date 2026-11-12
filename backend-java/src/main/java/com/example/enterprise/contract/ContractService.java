package com.example.enterprise.contract;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.contract.ContractRepository.ContractQuery;
import com.example.enterprise.contract.ContractRepository.ContractRow;
import com.example.enterprise.contract.ContractViews.ContractFileView;
import com.example.enterprise.contract.ContractViews.ContractPage;
import com.example.enterprise.contract.ContractViews.ContractView;
import com.example.enterprise.contract.ContractViews.LinkFile;
import com.example.enterprise.contract.ContractViews.WriteContract;
import com.example.enterprise.file.StoredFileRepository;
import com.example.enterprise.identity.TenantContext;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.Set;
import java.util.UUID;

@Service
@Profile("dev")
public class ContractService {

    private static final Set<String> FILE_ROLES = Set.of("original", "attachment", "supplement", "other");

    private final ContractRepository contracts;
    private final StoredFileRepository files;

    public ContractService(ContractRepository contracts, StoredFileRepository files) {
        this.contracts = contracts;
        this.files = files;
    }

    @Transactional
    public ContractView create(AuthenticatedUser user, WriteContract request) {
        UUID tenantId = openTenant(user);
        Validated fields = validate(request);
        UUID id = UUID.randomUUID();
        contracts.insert(new ContractRow(
                id, tenantId, fields.contractNo(), fields.title(), fields.contractType(), fields.counterpartyName(),
                fields.amount(), fields.currency(), fields.signDate(), fields.effectiveDate(), fields.expiryDate(),
                ContractStates.DRAFT, fields.description(), user.userId(), user.userId(), Instant.now(), Instant.now()));
        return get(user, id);
    }

    @Transactional
    public ContractView get(AuthenticatedUser user, UUID contractId) {
        UUID tenantId = openTenant(user);
        ContractRow row = requireContract(tenantId, contractId);
        return ContractRepository.toView(row, contracts.listFiles(tenantId, contractId));
    }

    @Transactional
    public ContractPage list(
            AuthenticatedUser user,
            String contractNo,
            String contractName,
            String counterpartyName,
            String contractType,
            String status,
            LocalDate signDateFrom,
            LocalDate signDateTo,
            int page,
            int size) {
        if (page < 0 || size < 1 || size > 100) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        if (status != null && !status.isBlank() && !ContractStates.known(status)) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        UUID tenantId = openTenant(user);
        ContractQuery query = new ContractQuery(
                tenantId,
                blankToNull(contractNo),
                like(contractName),
                like(counterpartyName),
                blankToNull(contractType),
                blankToNull(status),
                signDateFrom,
                signDateTo);
        List<ContractView> items = contracts.list(query, size, page * size);
        return new ContractPage(items, page, size, contracts.count(query));
    }

    @Transactional
    public ContractView update(AuthenticatedUser user, UUID contractId, WriteContract request) {
        UUID tenantId = openTenant(user);
        ContractRow current = requireContract(tenantId, contractId);
        if (!ContractStates.canEdit(current.status())) {
            throw new ContractException(HttpStatus.CONFLICT, "当前状态不允许修改");
        }
        Validated fields = validate(request);
        boolean updated = contracts.update(new ContractRow(
                contractId, tenantId, fields.contractNo(), fields.title(), fields.contractType(),
                fields.counterpartyName(), fields.amount(), fields.currency(), fields.signDate(),
                fields.effectiveDate(), fields.expiryDate(), current.status(), fields.description(),
                current.createdBy(), user.userId(), current.createdAt(), Instant.now()));
        if (!updated) {
            throw new ContractException(HttpStatus.CONFLICT, "当前状态不允许修改");
        }
        return get(user, contractId);
    }

    @Transactional
    public ContractView changeStatus(AuthenticatedUser user, UUID contractId, String target) {
        UUID tenantId = openTenant(user);
        ContractRow current = requireContract(tenantId, contractId);
        if (!ContractStates.known(target)) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        ContractStates.requireTransition(current.status(), target);
        if (!contracts.updateStatus(tenantId, contractId, current.status(), target, user.userId())) {
            throw new ContractException(HttpStatus.CONFLICT, "不允许的状态变更");
        }
        return get(user, contractId);
    }

    @Transactional
    public ContractFileView linkFile(AuthenticatedUser user, UUID contractId, LinkFile request) {
        UUID tenantId = openTenant(user);
        requireContract(tenantId, contractId);
        if (request == null || request.fileId() == null || !FILE_ROLES.contains(request.fileRole())) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        files.find(tenantId, request.fileId())
                .filter(file -> "ready".equals(file.status()))
                .orElseThrow(() -> new ContractException(HttpStatus.NOT_FOUND, "文件不存在"));
        contracts.linkFile(UUID.randomUUID(), tenantId, contractId, request.fileId(), request.fileRole(), user.userId());
        return contracts.listFiles(tenantId, contractId).stream()
                .filter(file -> file.fileId().equals(request.fileId()))
                .findFirst()
                .orElseThrow(() -> new ContractException(HttpStatus.NOT_FOUND, "文件不存在"));
    }

    @Transactional
    public List<ContractFileView> listFiles(AuthenticatedUser user, UUID contractId) {
        UUID tenantId = openTenant(user);
        requireContract(tenantId, contractId);
        return contracts.listFiles(tenantId, contractId);
    }

    @Transactional
    public void unlinkFile(AuthenticatedUser user, UUID contractId, UUID fileId) {
        UUID tenantId = openTenant(user);
        requireContract(tenantId, contractId);
        if (!contracts.unlinkFile(tenantId, contractId, fileId)) {
            throw new ContractException(HttpStatus.NOT_FOUND, "文件未关联");
        }
    }

    private ContractRow requireContract(UUID tenantId, UUID contractId) {
        return contracts.find(tenantId, contractId)
                .orElseThrow(() -> new ContractException(HttpStatus.NOT_FOUND, "合同不存在"));
    }

    private UUID openTenant(AuthenticatedUser user) {
        UUID tenantId = TenantContext.require();
        if (!tenantId.equals(user.tenantId())) {
            throw new ContractException(HttpStatus.FORBIDDEN, "租户上下文未建立");
        }
        contracts.applyTenant(tenantId);
        files.applyTenant(tenantId);
        return tenantId;
    }

    private static Validated validate(WriteContract request) {
        if (request == null) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        String contractNo = required(request.contractNo(), 64);
        String title = required(request.title(), 200);
        String contractType = required(request.contractType(), 50);
        String counterparty = required(request.counterpartyName(), 200);
        BigDecimal amount = normalizeAmount(request.amount());
        String currency = request.currency() == null ? "" : request.currency().trim().toUpperCase();
        if (!currency.matches("[A-Z]{3}")) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        if (request.effectiveDate() != null && request.expiryDate() != null
                && request.effectiveDate().isAfter(request.expiryDate())) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "日期不合法");
        }
        String description = request.description() == null || request.description().isBlank()
                ? null
                : request.description().trim();
        if (description != null && description.length() > 2000) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        return new Validated(contractNo, title, contractType, counterparty, amount, currency,
                request.signDate(), request.effectiveDate(), request.expiryDate(), description);
    }

    private static BigDecimal normalizeAmount(BigDecimal amount) {
        if (amount == null) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        BigDecimal normalized = amount.stripTrailingZeros();
        if (normalized.scale() < 0) {
            normalized = normalized.setScale(0);
        }
        if (normalized.signum() < 0 || normalized.scale() > 2 || normalized.precision() - normalized.scale() > 16) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "合同金额不合法");
        }
        return normalized.setScale(2);
    }

    private static String required(String value, int max) {
        if (value == null || value.isBlank() || value.trim().length() > max) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        return value.trim();
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.trim();
    }

    private static String like(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        return "%" + value.trim().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    }

    private record Validated(
            String contractNo,
            String title,
            String contractType,
            String counterpartyName,
            BigDecimal amount,
            String currency,
            LocalDate signDate,
            LocalDate effectiveDate,
            LocalDate expiryDate,
            String description) {
    }
}
