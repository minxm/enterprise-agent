package com.example.enterprise.file;

import java.util.regex.Pattern;

final class FileNames {

    private static final Pattern UNSAFE = Pattern.compile("[\\p{Cntrl}\":*?<>|]");

    private FileNames() {
    }

    static String baseName(String originalName) {
        String name = originalName == null ? "" : originalName.replace('\\', '/');
        int slash = name.lastIndexOf('/');
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        return name;
    }

    static String displayName(String originalName, String extension) {
        String name = UNSAFE.matcher(baseName(originalName)).replaceAll("");
        name = name.replace("..", "").trim();
        if (name.isBlank() || ".".equals(name) || name.length() > 200) {
            return "file." + extension;
        }
        return name;
    }

    static String contentDisposition(String filename) {
        String ascii = filename.replaceAll("[^A-Za-z0-9._-]", "_");
        if (ascii.isBlank()) {
            ascii = "file";
        }
        String encoded = java.net.URLEncoder.encode(filename, java.nio.charset.StandardCharsets.UTF_8).replace("+", "%20");
        return "attachment; filename=\"" + ascii + "\"; filename*=UTF-8''" + encoded;
    }
}
