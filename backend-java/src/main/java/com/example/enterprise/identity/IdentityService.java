package com.example.enterprise.identity;

import com.example.enterprise.identity.IdentityViews.AssociationCheck;
import com.example.enterprise.identity.IdentityViews.PermissionView;
import com.example.enterprise.identity.IdentityViews.RoleView;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
@Profile("dev")
public class IdentityService {

    private static final Map<String, String> ROLE_NAMES = Map.of(
            "tenant_admin", "租户管理员",
            "contract_manager", "合同管理员",
            "approver", "审批人");

    private static final Map<String, List<String>> ROLE_PERMISSIONS = Map.of(
            "tenant_admin", List.of(
                    "tenant:manage", "contract:read", "contract:write",
                    "contract:submit", "approval:decide", "policy:manage"),
            "contract_manager", List.of("contract:read", "contract:write", "contract:submit"),
            "approver", List.of("contract:read", "approval:decide"));

    private final IdentityRepository repository;
    private final PasswordHasher passwords;

    public IdentityService(IdentityRepository repository, PasswordHasher passwords) {
        this.repository = repository;
        this.passwords = passwords;
    }

    @Transactional
    public TenantView createTenant(String code, String name) {
        requireText(code, "租户编码不能为空");
        requireText(name, "租户名称不能为空");
        UUID tenantId = UUID.randomUUID();
        repository.insertTenant(tenantId, code.trim(), name.trim());
        repository.applyTenant(tenantId);
        for (Map.Entry<String, String> role : ROLE_NAMES.entrySet()) {
            UUID roleId = UUID.randomUUID();
            repository.insertRole(roleId, tenantId, role.getKey(), role.getValue());
            for (String permissionCode : ROLE_PERMISSIONS.get(role.getKey())) {
                repository.insertRolePermission(roleId, repository.findPermissionId(permissionCode), tenantId);
            }
        }
        return requireTenant(tenantId);
    }

    @Transactional
    public UserView createUser(String loginName, String rawPassword, String displayName) {
        UUID tenantId = openTenant();
        requireActiveTenant(tenantId);
        requireText(loginName, "登录名不能为空");
        requireText(displayName, "姓名不能为空");
        String passwordHash = passwords.hash(rawPassword);
        UUID userId = UUID.randomUUID();
        repository.insertUser(userId, tenantId, loginName.trim(), passwordHash, displayName.trim());
        return repository.findUser(userId).orElseThrow(() -> new IdentityException(HttpStatus.NOT_FOUND, "用户不存在"));
    }

    @Transactional
    public void assignRole(UUID userId, UUID roleId) {
        UUID tenantId = openTenant();
        requireVisibleUser(userId);
        requireVisibleRole(roleId);
        if (repository.userRoleExists(userId, roleId)) {
            throw new IdentityException(HttpStatus.CONFLICT, "用户角色关联已存在");
        }
        repository.insertUserRole(userId, roleId, tenantId);
    }

    @Transactional
    public TenantView currentTenant() {
        return requireTenant(openTenant());
    }

    @Transactional
    public List<UserView> listUsers() {
        openTenant();
        return repository.listUsers();
    }

    @Transactional
    public List<RoleView> listRoles(UUID userId) {
        openTenant();
        requireVisibleUser(userId);
        return repository.listRolesForUser(userId);
    }

    @Transactional
    public List<PermissionView> listPermissions(UUID roleId) {
        openTenant();
        requireVisibleRole(roleId);
        return repository.listPermissionsForRole(roleId);
    }

    @Transactional
    public AssociationCheck checkUserRole(UUID userId, UUID roleId) {
        UUID tenantId = openTenant();
        if (!"active".equals(requireTenant(tenantId).status())) {
            return new AssociationCheck(false, "租户已禁用");
        }
        if (repository.findUser(userId).isEmpty() || repository.findRole(roleId).isEmpty()) {
            return new AssociationCheck(false, "用户或角色不存在");
        }
        if (repository.userRoleExists(userId, roleId)) {
            return new AssociationCheck(false, "用户角色关联已存在");
        }
        return new AssociationCheck(true, null);
    }

    private UUID openTenant() {
        UUID tenantId = TenantContext.require();
        repository.applyTenant(tenantId);
        return tenantId;
    }

    private TenantView requireTenant(UUID tenantId) {
        return repository.findTenant(tenantId).orElseThrow(() -> new IdentityException(HttpStatus.NOT_FOUND, "租户不存在"));
    }

    private void requireActiveTenant(UUID tenantId) {
        if (!"active".equals(requireTenant(tenantId).status())) {
            throw new IdentityException(HttpStatus.CONFLICT, "租户已禁用");
        }
    }

    private void requireVisibleUser(UUID userId) {
        if (repository.findUser(userId).isEmpty()) {
            throw new IdentityException(HttpStatus.NOT_FOUND, "用户不存在");
        }
    }

    private void requireVisibleRole(UUID roleId) {
        if (repository.findRole(roleId).isEmpty()) {
            throw new IdentityException(HttpStatus.NOT_FOUND, "角色不存在");
        }
    }

    private static void requireText(String value, String message) {
        if (value == null || value.isBlank()) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, message);
        }
    }
}
