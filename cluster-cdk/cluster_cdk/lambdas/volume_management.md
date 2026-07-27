# Volume Management Lambda

## General Flow

```mermaid
flowchart TD
    A[(Volumes)] -->|Get Volume| B([Volume])
    B --> C{User Claim?}
    C -->|No| A
    C -->|Yes| D{Is Delete\n Protected?}
    D -->|Yes| A
    D -->|No| E{Is there\n a snapshot?}
    E -->|No| H(Warn missing snapshot)
    H --> A
    E -->|Yes| F{Is Expired?}
    F -->|No| A
    F -->|Yes| G(Delete PVC via kubectl)
    G --> A
    A -->|No More| J[(Snapshots)]
    J -->|Get Snapshot| K([Snapshot])
    K --> L{User Claim?}
    L -->|No| J
    L -->|Yes| M{Current time after `snapshot-delete-time`?}
    M -->|Yes| M1{Has grace\n period expired?}
    
    M1 -->|Yes| M2(Delete Snapshot)
    M2 --> J
    M1 -->|No| M3{Has Delete\n email tag?}
    
    M3 -->|Yes| J
    M3 -->|No| M4(Send Delete Email Notification)
    M4 --> M5(Add delete email Tag)
    M5 --> J
   
    M -->|No| N{Current time greater than the time to send next warning?}
    N -->|No| J
    N -->|Yes| O(Send Expiring Email)
    O --> P(Update last warning sent date tag)
    P --> J
```
