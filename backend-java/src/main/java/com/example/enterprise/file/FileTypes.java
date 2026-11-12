package com.example.enterprise.file;

import org.springframework.http.HttpStatus;

import java.io.ByteArrayInputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

final class FileTypes {

    static final String PDF = "application/pdf";
    static final String DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    static final String DOC = "application/msword";
    static final String TXT = "text/plain; charset=UTF-8";

    private FileTypes() {
    }

    record Detected(String extension, String mediaType) {
    }

    static Detected detect(String originalName, byte[] content) {
        String extension = extensionOf(originalName);
        return switch (extension) {
            case "pdf" -> require(isPdf(content), "pdf", PDF);
            case "docx" -> require(isDocx(content), "docx", DOCX);
            case "doc" -> require(isDoc(content), "doc", DOC);
            case "txt" -> require(isText(content), "txt", TXT);
            default -> throw unsupported();
        };
    }

    static String extensionOf(String originalName) {
        String name = FileNames.baseName(originalName).toLowerCase(Locale.ROOT);
        int dot = name.lastIndexOf('.');
        if (dot < 0 || dot == name.length() - 1) {
            return "";
        }
        String extension = name.substring(dot + 1);
        if (!extension.chars().allMatch(character -> character >= 'a' && character <= 'z' || character >= '0' && character <= '9')) {
            return "";
        }
        return extension;
    }

    private static Detected require(boolean accepted, String extension, String mediaType) {
        if (!accepted) {
            throw unsupported();
        }
        return new Detected(extension, mediaType);
    }

    private static boolean isPdf(byte[] content) {
        return startsWith(content, new byte[] {'%', 'P', 'D', 'F'});
    }

    private static boolean isDoc(byte[] content) {
        return startsWith(content, new byte[] {
                (byte) 0xD0, (byte) 0xCF, (byte) 0x11, (byte) 0xE0,
                (byte) 0xA1, (byte) 0xB1, (byte) 0x1A, (byte) 0xE1
        });
    }

    private static boolean isDocx(byte[] content) {
        if (!startsWith(content, new byte[] {'P', 'K', 3, 4})) {
            return false;
        }
        try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(content))) {
            int scanned = 0;
            for (ZipEntry entry = zip.getNextEntry(); entry != null && scanned < 32; entry = zip.getNextEntry()) {
                scanned++;
                String name = entry.getName();
                if (name.contains("..") || name.startsWith("/") || name.startsWith("\\")) {
                    return false;
                }
                if ("word/document.xml".equals(name)) {
                    return true;
                }
            }
            return false;
        } catch (Exception exception) {
            return false;
        }
    }

    private static boolean isText(byte[] content) {
        if (isPdf(content) || isDoc(content) || startsWith(content, new byte[] {'P', 'K'})) {
            return false;
        }
        for (byte value : content) {
            if (value == 0) {
                return false;
            }
        }
        try {
            StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(content));
            return true;
        } catch (CharacterCodingException exception) {
            return false;
        }
    }

    private static boolean startsWith(byte[] content, byte[] prefix) {
        if (content.length < prefix.length) {
            return false;
        }
        for (int index = 0; index < prefix.length; index++) {
            if (content[index] != prefix[index]) {
                return false;
            }
        }
        return true;
    }

    private static FileException unsupported() {
        return new FileException(HttpStatus.BAD_REQUEST, "不支持的文件类型");
    }
}
