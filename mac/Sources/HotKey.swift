import Carbon.HIToolbox
import Foundation

/// A system-wide hot key.
///
/// Uses Carbon's `RegisterEventHotKey` deliberately: unlike an
/// `NSEvent` global monitor it needs no Accessibility permission, so the app
/// does not have to ask for control of the machine just to be summonable.
final class HotKey {
    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: () -> Void
    private static var registry: [UInt32: HotKey] = [:]
    private static var nextID: UInt32 = 1

    /// - Parameters:
    ///   - keyCode: a virtual key code, e.g. `kVK_Space`.
    ///   - modifiers: Carbon modifier mask, e.g. `UInt32(optionKey)`.
    init?(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
        self.action = action

        let id = HotKey.nextID
        HotKey.nextID += 1
        HotKey.registry[id] = self

        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            HotKey.registry[hkID.id]?.action()
            return noErr
        }, 1, &eventType, nil, &handler)

        let hotKeyID = EventHotKeyID(signature: OSType(0x504C4149), id: id) // 'PLAI'
        let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID,
                                         GetApplicationEventTarget(), 0, &ref)
        if status != noErr { return nil }
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
        if let handler { RemoveEventHandler(handler) }
    }
}
