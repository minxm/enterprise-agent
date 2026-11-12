package com.example.enterprise.contract;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.contract.ContractViews.ContractFileView;
import com.example.enterprise.contract.ContractViews.ContractPage;
import com.example.enterprise.contract.ContractViews.ContractView;
import com.example.enterprise.contract.ContractViews.LinkFile;
import com.example.enterprise.contract.ContractViews.StatusChange;
import com.example.enterprise.contract.ContractViews.WriteContract;
import org.springframework.context.annotation.Profile;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDate;
import java.util.List;
import java.util.UUID;

@RestController
@Profile("dev")
@RequestMapping("/api/v1/contracts")
public class ContractController {

    private final ContractService contracts;

    public ContractController(ContractService contracts) {
        this.contracts = contracts;
    }

    @PostMapping
    public ApiResponse<ContractView> create(@RequestBody WriteContract request) {
        return ApiResponse.success(contracts.create(currentUser(), request));
    }

    @GetMapping("/{contractId}")
    public ApiResponse<ContractView> get(@PathVariable UUID contractId) {
        return ApiResponse.success(contracts.get(currentUser(), contractId));
    }

    @GetMapping
    public ApiResponse<ContractPage> list(
            @RequestParam(required = false) String contractNo,
            @RequestParam(required = false) String contractName,
            @RequestParam(required = false) String counterpartyName,
            @RequestParam(required = false) String contractType,
            @RequestParam(required = false) String status,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE) LocalDate signDateFrom,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE) LocalDate signDateTo,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size) {
        return ApiResponse.success(contracts.list(
                currentUser(), contractNo, contractName, counterpartyName, contractType, status,
                signDateFrom, signDateTo, page, size));
    }

    @PutMapping("/{contractId}")
    public ApiResponse<ContractView> update(@PathVariable UUID contractId, @RequestBody WriteContract request) {
        return ApiResponse.success(contracts.update(currentUser(), contractId, request));
    }

    @PostMapping("/{contractId}/status")
    public ApiResponse<ContractView> changeStatus(@PathVariable UUID contractId, @RequestBody StatusChange request) {
        return ApiResponse.success(contracts.changeStatus(currentUser(), contractId, request == null ? null : request.status()));
    }

    @PostMapping("/{contractId}/files")
    public ApiResponse<ContractFileView> linkFile(@PathVariable UUID contractId, @RequestBody LinkFile request) {
        return ApiResponse.success(contracts.linkFile(currentUser(), contractId, request));
    }

    @GetMapping("/{contractId}/files")
    public ApiResponse<List<ContractFileView>> listFiles(@PathVariable UUID contractId) {
        return ApiResponse.success(contracts.listFiles(currentUser(), contractId));
    }

    @DeleteMapping("/{contractId}/files/{fileId}")
    public ApiResponse<Void> unlinkFile(@PathVariable UUID contractId, @PathVariable UUID fileId) {
        contracts.unlinkFile(currentUser(), contractId, fileId);
        return ApiResponse.success(null);
    }

    private static AuthenticatedUser currentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            throw new ContractException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        return user;
    }
}
