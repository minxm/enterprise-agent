package com.example.enterprise.auth;

import com.example.enterprise.identity.IdentityViews.TenantView;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.Optional;
import java.util.UUID;

@Repository
@Profile("dev")
public class AuthRepository {

    private final JdbcTemplate jdbc;

    public AuthRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<TenantView> findTenantById(java.util.UUID id) {
        return jdbc.query("""
                select id, code, name, status
                from business.tenant
                where id = ?
                """, (rs, rowNum) -> new TenantView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name"),
                rs.getString("status")), id).stream().findFirst();
    }

    public Optional<TenantView> findTenantByCode(String code) {
        return jdbc.query("""
                select id, code, name, status
                from business.tenant
                where code = ?
                """, (rs, rowNum) -> new TenantView(
                rs.getObject("id", UUID.class),
                rs.getString("code"),
                rs.getString("name"),
                rs.getString("status")), code).stream().findFirst();
    }

    public void applyTenant(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
    }

    public Optional<StoredUser> findUserByLogin(String loginName) {
        return jdbc.query("""
                select id, tenant_id, login_name, display_name, status, password_hash
                from business.app_user
                where login_name = ?
                """, (rs, rowNum) -> new StoredUser(
                rs.getObject("id", UUID.class),
                rs.getObject("tenant_id", UUID.class),
                rs.getString("login_name"),
                rs.getString("display_name"),
                rs.getString("status"),
                rs.getString("password_hash")), loginName).stream().findFirst();
    }

    public Optional<StoredUser> findUserById(UUID userId) {
        return jdbc.query("""
                select id, tenant_id, login_name, display_name, status, password_hash
                from business.app_user
                where id = ?
                """, (rs, rowNum) -> new StoredUser(
                rs.getObject("id", UUID.class),
                rs.getObject("tenant_id", UUID.class),
                rs.getString("login_name"),
                rs.getString("display_name"),
                rs.getString("status"),
                rs.getString("password_hash")), userId).stream().findFirst();
    }

    public record StoredUser(
            UUID id,
            UUID tenantId,
            String loginName,
            String displayName,
            String status,
            String passwordHash) {
    }
}
