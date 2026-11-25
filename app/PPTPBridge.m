#import <Cocoa/Cocoa.h>
#import <Security/Security.h>
#import <unistd.h>

static int keychain(BOOL save, const char *account) {
    const char service[] = "org.pptpbridge.credentials";
    if (strcmp(account, "VPN") && strcmp(account, "SMB")) return 2;
    OSStatus status;
    if (save) {
        char password[1024];
        if (!fgets(password, sizeof(password), stdin)) return 2;
        size_t n = strcspn(password, "\r\n");
        if (n == 0 || n == sizeof(password) - 1) return 2;
        status = SecKeychainAddGenericPassword(NULL, sizeof(service)-1, service, strlen(account), account,
                                               (UInt32)n, password, NULL);
        if (status == errSecDuplicateItem) {
            SecKeychainItemRef item = NULL;
            status = SecKeychainFindGenericPassword(NULL, sizeof(service)-1, service, strlen(account), account,
                                                    NULL, NULL, &item);
            if (status == errSecSuccess) {
                status = SecKeychainItemModifyAttributesAndData(item, NULL, (UInt32)n, password);
                CFRelease(item);
            }
        }
        memset_s(password, sizeof(password), 0, sizeof(password));
    } else {
        if (isatty(STDOUT_FILENO)) return 2;
        SecKeychainSetUserInteractionAllowed(false);
        UInt32 len = 0;
        void *data = NULL;
        status = SecKeychainFindGenericPassword(NULL, sizeof(service)-1, service, strlen(account), account,
                                                &len, &data, NULL);
        if (status == errSecSuccess) {
            BOOL ok = fwrite(data, 1, len, stdout) == len;
            SecKeychainItemFreeContent(NULL, data);
            if (!ok) return 1;
        }
    }
    if (status != errSecSuccess) {
        NSString *reason = CFBridgingRelease(SecCopyErrorMessageString(status, NULL));
        fprintf(stderr, "Связка ключей: %s (%d)\n", reason.UTF8String, (int)status);
        return 1;
    }
    return 0;
}

@interface VPNDelegate : NSObject <NSApplicationDelegate>
@property(strong) NSWindow *window;
@property(strong) NSTextField *label;
@end

@implementation VPNDelegate
- (void)applicationDidFinishLaunching:(NSNotification *)note {
    NSURL *resources = NSBundle.mainBundle.resourceURL;
    NSString *profile = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/PPTP Bridge/configured"];
    if (![[NSFileManager defaultManager] fileExistsAtPath:profile]) {
        [[NSWorkspace sharedWorkspace] openURL:[resources URLByAppendingPathComponent:@"Setup.command"]];
        [NSApp terminate:nil];
        return;
    }
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0,0,370,80)
                  styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"PPTP Bridge";
    self.label = [NSTextField labelWithString:@"Переключаю VPN…"];
    self.label.frame = NSMakeRect(16,22,338,30);
    self.label.alignment = NSTextAlignmentCenter;
    self.label.font = [NSFont systemFontOfSize:17];
    [self.window.contentView addSubview:self.label];
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0), ^{
        NSString *script = [[resources URLByAppendingPathComponent:@"control.py"] path];
        NSTask *task = [[NSTask alloc] init];
        task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/python3"];
        task.arguments = @[@"-I", script, @"toggle"];
        NSPipe *output = [NSPipe pipe];
        task.standardOutput = output;
        task.standardError = output;
        task.standardInput = [NSFileHandle fileHandleWithNullDevice];
        NSError *error = nil;
        BOOL started = [task launchAndReturnError:&error];
        NSString *message = error.localizedDescription;
        BOOL ok = NO;
        if (started) {
            NSData *data = [output.fileHandleForReading readDataToEndOfFile];
            [task waitUntilExit];
            message = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
            ok = task.terminationStatus == 0;
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            if (ok) {
                self.label.stringValue = [message componentsSeparatedByString:@"\n"].firstObject ?: @"Готово";
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2*NSEC_PER_SEC), dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
            } else {
                [self.window orderOut:nil];
                NSAlert *alert = [[NSAlert alloc] init];
                alert.messageText = @"Не удалось переключить PPTP Bridge";
                alert.informativeText = message ?: @"Проверьте подключение к сети.";
                [alert runModal];
                [NSApp terminate:nil];
            }
        });
    });
}
@end

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc == 3 && !strcmp(argv[1], "--store")) return keychain(YES, argv[2]);
        if (argc == 3 && !strcmp(argv[1], "--read")) return keychain(NO, argv[2]);
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        VPNDelegate *delegate = [VPNDelegate new];
        app.delegate = delegate;
        [app run];
        (void)delegate.window;
    }
    return 0;
}
