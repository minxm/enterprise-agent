package com.example.enterprise.identity;

import com.example.enterprise.identity.IdentityViews.PermissionView;
import com.example.enterprise.identity.IdentityViews.RoleView;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import org.springframework.context.annotation.Profile;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Repository
@Profile("dev")
public class IdentityRepository {

    private final JdbcTemplate jdbc;

    public IdentityRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void applyTenant(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
    }

    public void insertTenant(UUID id, String code, String name) {
        jdbc.update("""
                insert into business.tenant (id, code, name, status)
                values (?, ?, ?, 'active')
                """, id, code, name);
    }

    public Optional<TenantView> findTenant(UUID id) {
        List<TenantView> rows = jdbc.query("""
                select id, code, name, status
                from business.tenant
                where id = ?
                """, (rs, rowNum) -> new TenantView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name"),
                rs.getString("status")), id);
        return rows.stream().findFirst();
    }

    public void insertUser(UUID id, UUID tenantId, String loginName, String passwordHash, String displayName) {
        write(() -> jdbc.update("""
                insert into business.app_user (id, tenant_id, login_name, password_hash, display_name, status)
                values (?, ?, ?, ?, ?, 'active')
                """, id, tenantId, loginName, passwordHash, displayName));
    }

    public Optional<String> findPasswordHash(UUID userId) {
        List<String> rows = jdbc.query("""
                select password_hash
                from business.app_user
                where id = ?
                """, (rs, rowNum) -> rs.getString(1), userId);
        return rows.stream().findFirst();
    }

    public List<UserView> listUsers() {
        return jdbc.query("""
                select id, login_name, display_name, status
                from business.app_user
                order by login_name
                """, (rs, rowNum) -> new UserView(
                rs.getObject("id", UUID.class),
                rs.getString("login_name"),
                rs.getString("display_name"),
                rs.getString("status")));
    }

    public Optional<UserView> findUser(UUID userId) {
        return listUsers().stream().filter(user -> user.id().equals(userId)).findFirst();
    }

    public void insertRole(UUID id, UUID tenantId, String code, String name) {
        write(() -> jdbc.update("""
                insert into business.role (id, tenant_id, code, name)
                values (?, ?, ?, ?)
                """, id, tenantId, code, name));
    }

    public Optional<RoleView> findRoleByCode(String code) {
        List<RoleView> rows = jdbc.query("""
                select id, code, name
                from business.role
                where code = ?
                """, (rs, rowNum) -> new RoleView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name")), code);
        return rows.stream().findFirst();
    }

    public void updateTenantStatus(UUID id, String status) {
        jdbc.update("""
                update business.tenant
                set status = ?, updated_at = now()
                where id = ?
                """, status, id);
    }

    public void updateUserStatus(UUID id, String status) {
        jdbc.update("""
                update business.app_user
                set status = ?, updated_at = now()
                where id = ?
                """, status, id);
    }

    public Optional<RoleView> findRole(UUID roleId) {
        List<RoleView> rows = jdbc.query("""
                select id, code, name
                from business.role
                where id = ?
                """, (rs, rowNum) -> new RoleView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name")), roleId);
        return rows.stream().findFirst();
    }

    public List<RoleView> listRolesForUser(UUID userId) {
        return jdbc.query("""
                select r.id, r.code, r.name
                from business.user_role ur
                join business.role r on r.id = ur.role_id
                where ur.user_id = ?
                order by r.code
                """, (rs, rowNum) -> new RoleView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name")), userId);
    }

    public UUID findPermissionId(String code) {
        return jdbc.queryForObject("select id from business.permission where code = ?", UUID.class, code);
    }

    public void insertUserRole(UUID userId, UUID roleId, UUID tenantId) {
        write(() -> jdbc.update("""
                insert into business.user_role (user_id, role_id, tenant_id)
                values (?, ?, ?)
                """, userId, roleId, tenantId));
    }

    public boolean userRoleExists(UUID userId, UUID roleId) {
        Integer count = jdbc.queryForObject("""
                select count(*)
                from business.user_role
                where user_id = ? and role_id = ?
                """, Integer.class, userId, roleId);
        return count != null && count > 0;
    }

    public void insertRolePermission(UUID roleId, UUID permissionId, UUID tenantId) {
        write(() -> jdbc.update("""
                insert into business.role_permission (role_id, permission_id, tenant_id)
                values (?, ?, ?)
                """, roleId, permissionId, tenantId));
    }

    public List<PermissionView> listPermissionsForRole(UUID roleId) {
        return jdbc.query("""
                select p.id, p.code, p.name
                from business.role_permission rp
                join business.permission p on p.id = rp.permission_id
                where rp.role_id = ?
                order by p.code
                """, (rs, rowNum) -> new PermissionView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name")), roleId);
    }

    private void write(Runnable statement) {
        try {
            statement.run();
        } catch (DataIntegrityViolationException exception) {
            throw mapConstraint(exception);
        }
    }

    private static IdentityException mapConstraint(DataIntegrityViolationException exception) {
        String detail = exception.getMostSpecificCause().getMessage();
        String text = detail == null ? "" : detail;
        if (text.contains("app_user_login_unique")) {
            return new IdentityException(HttpStatus.CONFLICT, "同一租户内登录名已存在");
        }
        if (text.contains("role_code_unique")) {
            return new IdentityException(HttpStatus.CONFLICT, "同一租户内角色编码已存在");
        }
        if (text.contains("tenant_code_unique")) {
            return new IdentityException(HttpStatus.CONFLICT, "租户编码已存在");
        }
        if (text.contains("user_role_pkey")) {
            return new IdentityException(HttpStatus.CONFLICT, "用户角色关联已存在");
        }
        if (text.contains("role_permission_pkey")) {
            return new IdentityException(HttpStatus.CONFLICT, "角色权限关联已存在");
        }
        if (text.contains("user_role_user_fk") || text.contains("user_role_role_fk") || text.contains("role_permission_role_fk")) {
            return new IdentityException(HttpStatus.CONFLICT, "不能跨租户分配角色");
        }
        return new IdentityException(HttpStatus.CONFLICT, "数据约束冲突");
    }
}
