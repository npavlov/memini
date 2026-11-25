// Robot.m: a walking robot.
#import <Foundation/Foundation.h>

static const NSInteger kMaxSteps = 100;

@protocol Walker <NSObject>
- (void)walk:(NSInteger)steps;
@end

@interface Robot : NSObject <Walker>
@property (nonatomic, copy) NSString *name;
- (instancetype)initWithName:(NSString *)name;
+ (instancetype)robotNamed:(NSString *)name;
@end

@implementation Robot

- (instancetype)initWithName:(NSString *)name
{
    self = [super init];
    if (self) {
        _name = [name copy];
    }
    return self;
}

+ (instancetype)robotNamed:(NSString *)name
{
    return [[Robot alloc] initWithName:name];
}

/// Walk a number of steps, at most the limit.
- (void)walk:(NSInteger)steps
{
    NSInteger stepped = MIN(steps, kMaxSteps);
    NSLog(@"%@ walked %@", self.name, @(stepped));
}

@end

int RobotCount(void)
{
    return 1;
}
