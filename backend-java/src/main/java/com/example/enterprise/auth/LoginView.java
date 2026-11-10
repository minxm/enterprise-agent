package com.example.enterprise.auth;

import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;

public record LoginView(String token, UserView user, TenantView tenant) {
}
