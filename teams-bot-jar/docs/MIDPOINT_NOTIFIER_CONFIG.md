# midPoint Notifier Configuration for Teams Approval Workflow

## Problem

The Teams bot's approval card shows "the requested access" (placeholder) instead of the actual target (role/service name) and access duration. This is because midPoint's notifier doesn't extract these from the case before POSTing to the middleware.

## Solution

**Step 1: Extract target from the case assignment**

In midPoint 4.10.3, when a notifier fires, it has access to the `aCase` object (the approval case). The target (what was requested) lives in the case's assignment delta.

**Step 2: Add the middleware notifier**

Configure a new notifier in `systemConfiguration → notificationConfiguration → handler` that:
1. Fires on **access-request cases** (category: `user` + objectType in certain conditions)
2. Extracts `requestedItem` (target role/service name) and `accessDuration` from the assignment
3. Sends both to the middleware in the POST body

---

## Configuration in midPoint 4.10.3

### 1. Navigate to Configuration → System

- Go to **Configuration → System**.
- Expand **`systemConfiguration` → `notificationConfiguration` → `handler`**.

### 2. Add/update the notifier

Replace or add this entry under the `<handler>` list (in XML or import via REST):

```xml
<handler>
    <name>teamsApprovalNotifier</name>
    <displayName>Teams Middleware Approval Notifier</displayName>
    <description>Sends access-request events to the Teams connector middleware</description>
    <type>simpleCaseManagementNotifier</type>
    <enabled>true</enabled>
    
    <!-- Fire on approvals (work items opened) and completions (outcomes set) -->
    <event>
        <status>open</status>  <!-- Work item opened = approval requested -->
    </event>
    <event>
        <status>completed</status>  <!-- Work item closed -->
    </event>
    
    <!-- Condition: only user-approval cases (access requests) -->
    <category>user</category>
    
    <!-- HTML/text messages (optional, for email fallback) -->
    <transport>teamsMiddleware</transport>  <!-- Must match the custom transport name below -->
    
    <!-- Groovy bodyExpression extracts the target and builds the notification JSON -->
    <bodyExpression>
        <script>
            <code>
                // Map the event type based on work-item state
                def eventType = event?.eventType?.toString() ?: 'UNKNOWN'
                def eventName = 'ACCESS_REQUEST_RAISED'
                if (eventType.contains('completed') || aCase?.state()?.toString()?.contains('closed')) {
                    eventName = 'ACCESS_REQUEST_COMPLETED'
                }
                
                // Extract target (role/service/application) from the assignment
                def requestedItem = null
                def accessDuration = null
                if (aCase != null) {
                    def objectRef = aCase.objectRef()  // The user being assigned
                    def targetRef = aCase.targetRef()  // The role/service/org being assigned
                    def assignment = aCase.assignment()
                    
                    if (targetRef != null) {
                        // Get the target name (role, service, application name)
                        try {
                            def target = midpoint.getObject(targetRef.type(), targetRef.oid())
                            requestedItem = target?.name()?.toString() ?: targetRef.oid()
                        } catch (Exception e) {
                            // Fallback: use OID if object not found
                            requestedItem = targetRef.oid()
                        }
                    }
                    
                    // Extract accessUntil if present (convert to readable duration)
                    if (assignment?.activationDate() != null) {
                        def until = assignment.validTo()
                        if (until != null) {
                            def days = ((until.time - new Date().time) / (1000 * 60 * 60 * 24)).toInteger()
                            accessDuration = "${days} days"
                        }
                    } else {
                        accessDuration = 'Permanent'
                    }
                }
                
                // Extract manager (approver of the work item)
                def managerEmail = null
                if (workItem != null) {
                    def assigneeRef = workItem.assigneeRef()
                    if (assigneeRef != null) {
                        try {
                            def mgr = midpoint.getObject('UserType', assigneeRef.oid())
                            managerEmail = mgr.emailAddress()?.toString()
                        } catch (Exception e) {
                            // Fallback: use OID
                            managerEmail = assigneeRef.oid()
                        }
                    }
                }
                
                // Extract requester
                def requesterRef = aCase.objectRef()  // User requesting the access
                def requesterEmail = null
                def requesterName = null
                if (requesterRef != null) {
                    try {
                        def user = midpoint.getObject('UserType', requesterRef.oid())
                        requesterEmail = user.emailAddress()?.toString()
                        requesterName = user.fullName()?.toString()
                    } catch (Exception e) {
                        requesterEmail = requesterRef.oid()
                    }
                }
                
                // Build the JSON body for the middleware
                [
                    event: eventName,
                    caseOid: aCase?.oid()?.toString(),
                    workItemId: workItem?.id() ?: 1,
                    stageNumber: workItem?.stageNumber() ?: 1,
                    requesterEmail: requesterEmail,
                    requesterName: requesterName,
                    requestedItem: requestedItem,
                    managerEmail: managerEmail,
                    status: eventType,
                    accessDuration: accessDuration
                ] as String  // Return as JSON
            </code>
        </script>
    </bodyExpression>
    
    <!-- Plain text subject/body (optional, for email) -->
    <subjectExpression>
        <script>
            <code>
                def target = aCase?.targetRef()?.oid() ?: 'Unknown'
                def who = aCase?.objectRef()?.oid() ?: 'Someone'
                "Access request: ${who} → ${target}"
            </code>
        </script>
    </subjectExpression>
</handler>
```

### 3. Ensure the custom transport exists

In the same `systemConfiguration → notificationConfiguration → messageTransportConfiguration`, verify the `teamsMiddleware` transport exists:

```xml
<messageTransportConfiguration>
    <name>teamsMiddleware</name>
    <type>http</type>
    <url>${connector.notify_url}</url>  <!-- Points to middleware /notify endpoint -->
    <transport>
        <http>
            <method>POST</method>
            <username/>  <!-- Leave blank -->
            <password/>  <!-- Leave blank -->
            <!-- Authentication via header below -->
            <header>
                <name>X-Api-Key</name>
                <expression>
                    <script>
                        <code>${connector.notify_token}</code>
                    </script>
                </expression>
            </header>
            <contentType>application/json</contentType>
        </http>
    </transport>
</messageTransportConfiguration>
```

### 4. (Optional) Scope the notifier to access-request cases only

Add a **condition** to the notifier to fire only on user-access cases (not all cases):

```xml
<condition>
    <script>
        <code>
            // Fire only on approvals for users (access requests), not org-role changes
            aCase?.archetypeRef()?.find {
                it.oid()?.toString() == '...'  // UUID of "Access Request" archetype
            } != null
        </code>
    </script>
</condition>
```

(If you don't know the archetype OID, skip this step — it will fire on all cases, and the middleware's dedupe will handle duplicates.)

---

## Testing the notifier

1. **Submit an access request** from the Teams bot.
2. **Check midPoint logs** for `teamsMiddleware` or `NotificationManager` to see if the notifier fired:
   ```bash
   tail -f midpoint.log | grep -iE 'teamsMiddleware|NotificationManager|EventHandler'
   ```
3. **Verify the middleware received it**:
   ```bash
   tail -f connector.log | grep -i 'Forwarded.*for case'
   ```
4. **Check the Teams card** — it should now show the actual role/service name and duration.

---

## If the notifier still doesn't fire

See `docs/architecture.md § 4` ("THE critical-path blocker"). The blocker is still open; known workarounds:

1. **Use the legacy bot-side routing** (set `MANAGER_NOTIFY_MODE=bot` in the Python bot) — the bot queries midPoint to resolve the manager and DMs directly, bypassing the notifier.
2. **Configure a separate webhook** (AWS Lambda, logic app) to poll midPoint cases and POST to the middleware.

