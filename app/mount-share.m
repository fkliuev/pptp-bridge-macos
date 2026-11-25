#import <Foundation/Foundation.h>
#import <NetFS/NetFS.h>
#include <errno.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 3) return 2;
        NSString *user = [NSString stringWithUTF8String:argv[1]];
        NSURL *base = [NSURL URLWithString:@"smb://127.0.0.1:14446/"];
        NSURL *server = [base URLByAppendingPathComponent:[NSString stringWithUTF8String:argv[2]]];
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSString *password = [[NSString alloc] initWithData:input encoding:NSUTF8StringEncoding];
        if ([password hasSuffix:@"\n"])
            password = [password substringToIndex:password.length - 1];
        NSMutableDictionary *options = [@{
            (__bridge NSString *)kNetFSAllowLoopbackKey: @YES,
            (__bridge NSString *)kNAUIOptionKey: (__bridge NSString *)kNAUIOptionNoUI
        } mutableCopy];
        CFArrayRef paths = NULL;
        int error = NetFSMountURLSync((__bridge CFURLRef)server, NULL, (__bridge CFStringRef)user,
                                      (__bridge CFStringRef)password,
                                      (__bridge CFMutableDictionaryRef)options, NULL, &paths);
        NSArray *mounts = CFBridgingRelease(paths);
        if (error && error != EEXIST) {
            fprintf(stderr, "Не удалось подключить SMB (код SMB %d).\n", error);
            return 1;
        }
        if (mounts.count)
            puts([mounts.firstObject fileSystemRepresentation]);
        return 0;
    }
}
