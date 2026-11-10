package com.example.enterprise.identity;

import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.identity.IdentityViews.AssociationCheck;
import com.example.enterprise.identity.IdentityViews.PermissionView;
import com.example.enterprise.identity.IdentityViews.RoleView;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserRoleCheck;
import com.example.enterprise.identity.IdentityViews.UserView;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.UUID;

/**
 * 只读取服务端租户上下文。请求里的 tenantId 不参与授权。
 */
@RestController
@Profile("dev")
@RequestMapping("/api/v1/identity")
public class IdentityController {

    private final IdentityService identity;

    public IdentityController(IdentityService identity) {
        this.identity = identity;
    }

    @GetMapping("/tenant")
    public ApiResponse<TenantView> tenant() {
        return ApiResponse.success(identity.currentTenant());
    }

    @GetMapping("/users")
    public ApiResponse<List<UserView>> users() {
        return ApiResponse.success(identity.listUsers());
    }

    @GetMapping("/users/{userId}/roles")
    public ApiResponse<List<RoleView>> roles(@PathVariable UUID userId) {
        return ApiResponse.success(identity.listRoles(userId));
    }

    @GetMapping("/roles/{roleId}/permissions")
    public ApiResponse<List<PermissionView>> permissions(@PathVariable UUID roleId) {
        return ApiResponse.success(identity.listPermissions(roleId));
    }

    @PostMapping("/user-roles/check")
    public ApiResponse<AssociationCheck> check(@RequestBody UserRoleCheck request) {
        return ApiResponse.success(identity.checkUserRole(request.userId(), request.roleId()));
    }
}
