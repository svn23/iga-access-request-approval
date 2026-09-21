package in.cnxy.connector;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

@Service class MidpointClient {
  private final RestClient rest; private final ObjectMapper json;
  MidpointClient(ConnectorProperties p,ObjectMapper j){ json=j; URI uri=URI.create(p.midpointBaseUrl()); boolean loopbackHttp="http".equalsIgnoreCase(uri.getScheme()) && ("localhost".equalsIgnoreCase(uri.getHost()) || "127.0.0.1".equals(uri.getHost())); if(!"https".equalsIgnoreCase(uri.getScheme())&&!loopbackHttp) throw new IllegalStateException("MIDPOINT_BASE_URL must use HTTPS, except for localhost/127.0.0.1 development"); rest=RestClient.builder().baseUrl(p.midpointBaseUrl().replaceAll("/$", "")).defaultHeaders(h->{h.setBasicAuth(p.midpointUsername(),p.midpointPassword());h.setAccept(List.of(MediaType.APPLICATION_JSON));}).build(); }
  JsonNode get(String path){ return rest.get().uri(path).retrieve().body(JsonNode.class); }
  void patch(String path, JsonNode body){ rest.patch().uri(path).contentType(MediaType.APPLICATION_JSON).body(body).retrieve().toBodilessEntity(); }
  private JsonNode post(String path, JsonNode body){ return rest.post().uri(path).contentType(MediaType.APPLICATION_JSON).body(body).retrieve().body(JsonNode.class); }
  /** midPoint text-filter search body: {"query":{"filter":{"text":"<expr>"}}} */
  private ObjectNode textQuery(String expr){ ObjectNode q=json.createObjectNode(); q.putObject("query").putObject("filter").put("text",expr); return q; }
  List<CatalogItem> catalog(){ List<CatalogItem> out=new ArrayList<>(); add(out,post("/ws/rest/roles/search?options=resolveNames",textQuery("requestable = true")),"role"); add(out,post("/ws/rest/services/search?options=resolveNames",textQuery("requestable = true")),"service"); return out; }
  private void add(List<CatalogItem> out,JsonNode root,String type){ JsonNode list=root.path("object").path("object"); if(list.isArray()){ for(JsonNode node:list) addNode(out,node,type); } else if(list.isObject()){ addNode(out,list,type); } else if(root.isArray()){ for(JsonNode node:root) addNode(out,node,type); } }
  private void addNode(List<CatalogItem> out,JsonNode n,String type){ String oid=n.path("oid").asText(); String name=n.path("displayName").asText(n.path("name").asText()); if(!oid.isBlank()&&!name.isBlank()) out.add(new CatalogItem(type,name,oid,riskOf(name))); }

  /**
   * Requestable access menu: every RoleType and ServiceType flagged requestable=true
   * (folder "08 - List Requestable Access" in the Postman collection).
   * Returns typed items so the caller keeps the exact oid + midPoint type for a later request.
   */
  List<RequestableItem> requestableAccess(){
    List<RequestableItem> out=new ArrayList<>();
    addRequestable(out,post("/ws/rest/roles/search?options=resolveNames",textQuery("requestable = true")),"role","c:RoleType");
    addRequestable(out,post("/ws/rest/services/search?options=resolveNames",textQuery("requestable = true")),"service","c:ServiceType");
    return out;
  }
  private void addRequestable(List<RequestableItem> out,JsonNode root,String type,String midpointType){
    for(JsonNode n:iter(root.path("object").path("object"))){
      String oid=n.path("oid").asText(); String name=n.path("displayName").asText(n.path("name").asText());
      if(oid.isBlank()||name.isBlank()) continue;
      out.add(new RequestableItem(type,midpointType,name,oid,n.path("description").asText(""),riskOf(name)));
    }
  }
  /** Best-effort risk hint from the object name until midPoint exposes an explicit risk level. */
  private String riskOf(String name){ String l=name==null?"":name.toLowerCase(); return (l.contains("admin")||l.contains("root")||l.contains("owner")||l.contains("privileg"))?"high":"medium"; }
  String resolveUserOid(String email){ ObjectNode query=json.createObjectNode(); query.putObject("query").putObject("filter").putObject("equal").put("path","emailAddress").put("value",email); JsonNode r=rest.post().uri("/ws/rest/users/search").contentType(MediaType.APPLICATION_JSON).body(query).retrieve().body(JsonNode.class); JsonNode list=r.path("object").path("object"); if(list.isArray()){ for(JsonNode u:list){ if(u.hasNonNull("oid")) return u.path("oid").asText(); } } else if(list.isObject()&&list.hasNonNull("oid")){ return list.path("oid").asText(); } throw new IllegalArgumentException("No MidPoint user matches requester email"); }
  /** Resolve the logged-in user's midPoint profile by email (used when a Teams session starts). */
  UserProfile userProfile(String email){
    String oid=resolveUserOid(email);
    JsonNode u=get("/ws/rest/users/"+oid+"?options=resolveNames").path("user");
    String name=poly(u.path("name"));
    String fullName=poly(u.path("fullName"));
    String mail=u.path("emailAddress").asText(email);
    List<String> roles=new ArrayList<>();
    for(JsonNode a:iter(u.path("assignment"))){
      String tn=poly(a.path("targetRef").path("targetName"));
      if(!tn.isBlank()) roles.add(tn);
    }
    return new UserProfile(oid, name, fullName.isBlank()?name:fullName, mail, roles.size(), roles);
  }

  /**
   * Resolve the requester's line manager from midPoint: a user assigned to any of the
   * requester's parent orgs with the {@code org:manager} relation. Returns null if none.
   */
  ManagerRef managerOf(String email){
    String oid=resolveUserOid(email);
    JsonNode u=get("/ws/rest/users/"+oid+"?options=resolveNames").path("user");
    for(JsonNode p:iter(u.path("parentOrgRef"))){
      String orgOid=refOid(p);
      if(orgOid.isBlank()) continue;
      JsonNode r=post("/ws/rest/users/search?options=resolveNames",
          textQuery("assignment/targetRef matches (oid = \""+orgOid+"\" and relation = manager)"));
      for(JsonNode m:iter(r.path("object").path("object"))){
        String moid=m.path("oid").asText();
        if(moid.isBlank()||moid.equals(oid)) continue; // never route to self
        String mmail=m.path("emailAddress").asText("");
        String mname=poly(m.path("name"));
        String mfull=poly(m.path("fullName"));
        return new ManagerRef(moid, mname, mfull.isBlank()?mname:mfull, mmail);
      }
    }
    return null;
  }
  /** A midPoint PolyString serializes either as a plain string or as {"orig": "..."}. */
  private String poly(JsonNode node){ if(node==null||node.isMissingNode()||node.isNull()) return ""; return node.isTextual()?node.asText(""):node.path("orig").asText(""); }
  void assign(String userOid,String targetType,String targetOid,String until){ patch("/ws/rest/users/"+userOid, assignmentDelta("add", targetType, targetOid, until)); }
  void unassign(String userOid,String targetType,String targetOid){ patch("/ws/rest/users/"+userOid, assignmentDelta("delete", targetType, targetOid, null)); }
  private JsonNode assignmentDelta(String modificationType,String targetType,String targetOid,String until){ ObjectNode delta=json.createObjectNode(); ObjectNode mod=delta.putObject("objectModification"); ObjectNode item=mod.putObject("itemDelta"); item.put("modificationType",modificationType); item.put("path","assignment"); ObjectNode value=item.putObject("value"); ObjectNode targetRef=value.putObject("targetRef"); targetRef.put("oid",targetOid); targetRef.put("type", midPointType(targetType)); if(until!=null){ value.putObject("activation").put("validTo",until); } return delta; }
  String midPointType(String targetType){ String normalized=targetType==null?"role":targetType.trim().toLowerCase(); return switch(normalized){ case "group" -> "c:OrgType"; case "service" -> "c:ServiceType"; case "resource" -> "c:ResourceType"; default -> "c:RoleType"; }; }

  // --- Dynamic catalog: user -> orgs -> applications -> application roles ---

  /** Org memberships (assignment targetRef of type OrgType) for a user resolved by email. */
  List<CatalogOption> orgsForUser(String email){
    String oid=resolveUserOid(email);
    JsonNode u=get("/ws/rest/users/"+oid).path("user");
    List<CatalogOption> out=new ArrayList<>();
    for(JsonNode a:iter(u.path("assignment"))){
      JsonNode tr=a.path("targetRef");
      if(tr.path("type").asText("").endsWith("OrgType") && tr.hasNonNull("oid")) out.add(orgOption(tr.path("oid").asText()));
    }
    return out;
  }

  /** Applications (Service objects) assigned to any org the user belongs to. */
  List<CatalogOption> applicationsForUser(String email){
    List<CatalogOption> out=new ArrayList<>();
    java.util.Set<String> seen=new java.util.HashSet<>();
    for(CatalogOption org:orgsForUser(email)){
      for(CatalogOption app:searchByAssignment("/ws/rest/services/search", org.oid())){
        if(seen.add(app.oid())) out.add(app);
      }
    }
    return out;
  }

  /** Roles assigned to a given application service (its access levels). */
  List<CatalogOption> rolesForApplication(String applicationOid){
    return searchByAssignment("/ws/rest/roles/search", applicationOid);
  }

  private CatalogOption orgOption(String orgOid){
    JsonNode o=get("/ws/rest/orgs/"+orgOid).path("org");
    String name=o.path("displayName").asText(o.path("name").asText(orgOid));
    return new CatalogOption(name, orgOid);
  }

  private List<CatalogOption> searchByAssignment(String path, String targetOid){
    ObjectNode query=json.createObjectNode();
    ObjectNode ref=query.putObject("query").putObject("filter").putObject("ref");
    ref.put("path","assignment/targetRef");
    ref.putObject("value").put("oid",targetOid);
    JsonNode r=rest.post().uri(path).contentType(MediaType.APPLICATION_JSON).body(query).retrieve().body(JsonNode.class);
    List<CatalogOption> out=new ArrayList<>();
    JsonNode list=r.path("object").path("object");
    for(JsonNode n:iter(list)){
      String oid=n.path("oid").asText(); String name=n.path("displayName").asText(n.path("name").asText());
      if(!oid.isBlank() && !name.isBlank()) out.add(new CatalogOption(name, oid));
    }
    return out;
  }

  /** Iterate a MidPoint node that may be an array, a single object, or missing. */
  private Iterable<JsonNode> iter(JsonNode node){
    if(node.isArray()) return node;
    if(node.isObject()) return List.of(node);
    return List.of();
  }

  // --- Approval workflow (Cases + Work Items), mirroring the Access Request Postman collection ---

  private static final String OUTCOME_APPROVE="http://midpoint.evolveum.com/xml/ns/public/model/approval/outcome#approve";
  private static final String OUTCOME_REJECT ="http://midpoint.evolveum.com/xml/ns/public/model/approval/outcome#reject";

  /** OID of a reference, tolerating the resolveNames "t:oid" serialization. */
  private static String refOid(JsonNode ref){ if(ref==null||ref.isMissingNode()) return ""; String o=ref.path("oid").asText(""); return o.isBlank()?ref.path("t:oid").asText(""):o; }

  /**
   * Locate the open approval work item for a user's pending request on a target.
   * Searches open cases (MID-9493: the submit PATCH returns no case OID) and matches
   * client-side on the case objectRef (requesting user) and targetRef (requested role/service).
   */
  Optional<WorkItemRef> findOpenWorkItem(String userOid,String targetOid){
    JsonNode r=post("/ws/rest/cases/search?options=resolveNames",textQuery("state = \"open\""));
    for(JsonNode c:iter(r.path("object").path("object"))){
      List<JsonNode> workItems=new ArrayList<>(); iter(c.path("workItem")).forEach(workItems::add);
      if(workItems.isEmpty()) continue;
      boolean userMatch=userOid==null||userOid.isBlank()||refOid(c.path("objectRef")).equals(userOid);
      boolean targetMatch=targetOid==null||targetOid.isBlank()||refOid(c.path("targetRef")).equals(targetOid);
      if(userMatch&&targetMatch){
        JsonNode wi=workItems.get(0);
        String wid=wi.path("@id").asText(wi.path("id").asText());
        return Optional.of(new WorkItemRef(c.path("oid").asText(),wid));
      }
    }
    return Optional.empty();
  }

  /**
   * Count of open approval work items assigned to this user as approver/manager — the number the
   * greeting card shows ("N approval(s) waiting for your review"). Searches open cases and matches
   * each still-open work item's assigneeRef (single ref or array) against the user's oid.
   */
  int pendingApprovalsFor(String email){
    String oid=resolveUserOid(email);
    if(oid==null||oid.isBlank()) return 0;
    JsonNode r=post("/ws/rest/cases/search?options=resolveNames",textQuery("state = \"open\""));
    int count=0;
    for(JsonNode c:iter(r.path("object").path("object"))){
      for(JsonNode wi:iter(c.path("workItem"))){
        if(!wi.path("closeTimestamp").isMissingNode()) continue; // only open work items
        for(JsonNode a:iter(wi.path("assigneeRef"))){
          if(oid.equals(refOid(a))){ count++; break; }
        }
      }
    }
    return count;
  }

  /** Email address for a user oid ({@code emailAddress}); null if unresolvable. */
  String emailForOid(String oid){
    if(oid==null||oid.isBlank()) return null;
    try {
      JsonNode u=get("/ws/rest/users/"+oid);
      JsonNode user=u.has("user")?u.path("user"):u;
      String e=user.path("emailAddress").asText("");
      return e.isBlank()?null:e;
    } catch(RuntimeException ex){ return null; }
  }

  /**
   * Resolve the open approval for a user's pending request on a target, returning the case + work
   * item + the actual assignee (approver) emails midPoint routed it to. Used right after submit so
   * the bot can DM the real approver(s) — not an assumed org manager — and drive the card directly.
   */
  Optional<PendingApproval> resolvePendingApproval(String userOid, String targetOid){
    JsonNode r=post("/ws/rest/cases/search?options=resolveNames",textQuery("state = \"open\""));
    for(JsonNode c:iter(r.path("object").path("object"))){
      List<JsonNode> workItems=new ArrayList<>(); iter(c.path("workItem")).forEach(workItems::add);
      if(workItems.isEmpty()) continue;
      boolean userMatch=userOid==null||userOid.isBlank()||refOid(c.path("objectRef")).equals(userOid);
      boolean targetMatch=targetOid==null||targetOid.isBlank()||refOid(c.path("targetRef")).equals(targetOid);
      if(userMatch&&targetMatch){
        JsonNode wi=workItems.get(0);
        String wid=wi.path("@id").asText(wi.path("id").asText());
        List<String> approvers=new ArrayList<>();
        for(JsonNode a:iter(wi.path("assigneeRef"))){
          String em=emailForOid(refOid(a));
          if(em!=null && !approvers.contains(em)) approvers.add(em);
        }
        return Optional.of(new PendingApproval(c.path("oid").asText(), wid, approvers));
      }
    }
    return Optional.empty();
  }

  /** As {@link #resolvePendingApproval} but retried — MID-9493: the case may not be queryable the
   *  instant the assign PATCH returns, so poll a few times before concluding it was auto-executed. */
  Optional<PendingApproval> resolvePendingApprovalWithRetry(String userOid, String targetOid, int attempts, long delayMs){
    for(int i=0;i<Math.max(1,attempts);i++){
      Optional<PendingApproval> pa=resolvePendingApproval(userOid,targetOid);
      if(pa.isPresent()) return pa;
      if(i<attempts-1){ try{ Thread.sleep(delayMs); }catch(InterruptedException e){ Thread.currentThread().interrupt(); break; } }
    }
    return Optional.empty();
  }

  /** Complete a work item with the approve/reject outcome (POST …/cases/{oid}/workItems/{id}/complete). */
  void completeWorkItem(WorkItemRef ref,boolean approve,String comment){
    ObjectNode body=json.createObjectNode(); ObjectNode out=body.putObject("output");
    out.put("@type","c:AbstractWorkItemOutputType");
    out.put("comment",comment==null?"":comment);
    out.put("outcome",approve?OUTCOME_APPROVE:OUTCOME_REJECT);
    rest.post().uri("/ws/rest/cases/"+ref.caseOid()+"/workItems/"+ref.workItemId()+"/complete").contentType(MediaType.APPLICATION_JSON).body(body).retrieve().toBodilessEntity();
  }

  /**
   * Open approval cases (those with a requested target and at least one work item), keyed by case OID.
   * Snapshot used by {@link WorkItemPoller} to detect new work items and finished cases without
   * depending on midPoint's notifier/Groovy transport.
   */
  java.util.Map<String,OpenCase> openApprovalCases(){
    java.util.Map<String,OpenCase> out=new java.util.LinkedHashMap<>();
    JsonNode r=post("/ws/rest/cases/search?options=resolveNames",textQuery("state = \"open\""));
    for(JsonNode c:iter(r.path("object").path("object"))){
      String target=poly(c.path("targetRef").path("targetName"));
      if(target.isBlank()) continue; // operation/execution wrapper case, not a user request
      java.util.Map<String,List<String>> items=new java.util.LinkedHashMap<>();
      for(JsonNode wi:iter(c.path("workItem"))){
        if(!wi.path("closeTimestamp").isMissingNode()) continue;
        String wid=wi.path("@id").asText(wi.path("id").asText());
        if(wid.isBlank()) continue;
        List<String> approvers=new ArrayList<>();
        for(JsonNode a:iter(wi.path("assigneeRef"))){ String o=refOid(a); if(!o.isBlank()) approvers.add(o); }
        items.put(wid,approvers);
      }
      if(!items.isEmpty()) out.put(c.path("oid").asText(),new OpenCase(c.path("oid").asText(),refOid(c.path("objectRef")),target,items));
    }
    return out;
  }

  /** Retrieve a single case (used for verification / status). */
  JsonNode getCase(String caseOid){ return get("/ws/rest/cases/"+caseOid+"?options=resolveNames"); }

  /**
   * Resolve the actionable work item of a known case for the approval-card path. Authoritative for
   * delayed/concurrent approvals: a manager may click hours or days later, by which time the case
   * may have been decided by another approver, escalated, or auto-closed by an SLA/deadline policy.
   * Fetches the case once and reports whether a work item is still OPEN (with its ref), CLOSED
   * (exists but already decided/expired — clicking must NOT re-complete), or MISSING (no such case).
   *
   * <p>{@code preferredWorkItemId} (from the notification) is honoured when it names a still-open
   * work item; otherwise the first open work item is used. This makes the decision idempotent — a
   * second click lands on CLOSED, not a spurious 500.
   */
  CaseWorkItem resolveActionable(String caseOid, String preferredWorkItemId){
    if(caseOid==null||caseOid.isBlank()) return new CaseWorkItem("missing",null,null);
    JsonNode raw;
    try { raw=getCase(caseOid); }
    catch(RuntimeException e){ return new CaseWorkItem("missing",null,null); }
    // GET /ws/rest/cases/{oid} wraps the object as {"case": {...}} (like users → {"user":...}).
    JsonNode c = (raw!=null && raw.has("case")) ? raw.path("case") : raw;
    if(c==null||c.path("oid").asText("").isBlank()) return new CaseWorkItem("missing",null,null);
    boolean caseClosed="closed".equalsIgnoreCase(c.path("state").asText(""));
    // The requested target's name (resolveNames populates targetRef.targetName) — so the decision
    // receipt can label what was approved/rejected even if the caller's card lacked requestedItem.
    String targetName=poly(c.path("targetRef").path("targetName"));
    WorkItemRef open=null, preferred=null; boolean anyWorkItem=false;
    for(JsonNode wi:iter(c.path("workItem"))){
      anyWorkItem=true;
      String wid=wi.path("@id").asText(wi.path("id").asText());
      if(wid.isBlank()) continue;
      boolean itemOpen=wi.path("closeTimestamp").isMissingNode() && !caseClosed;
      if(itemOpen){
        WorkItemRef ref=new WorkItemRef(caseOid,wid);
        if(open==null) open=ref;
        if(preferredWorkItemId!=null && wid.equals(preferredWorkItemId)) preferred=ref;
      }
    }
    WorkItemRef pick = preferred!=null ? preferred : open;
    if(pick!=null) return new CaseWorkItem("open",pick,targetName);
    // No open work item: the case exists but is decided/expired (closed) if it had any; else missing.
    return new CaseWorkItem(anyWorkItem?"closed":"missing",null,targetName);
  }

  /**
   * Approval cases raised for a user — the source of truth for "my requests".
   * midPoint owns request state; the bot holds none. Returns target name, case state
   * (open | closed), and the raw outcome URI (present once closed).
   */
  List<RequestStatus> requestsFor(String email){
    List<RequestStatus> out=new ArrayList<>();
    String oid=resolveUserOid(email);
    if(oid==null||oid.isBlank()) return out;
    JsonNode r=post("/ws/rest/cases/search?options=resolveNames",
        textQuery("objectRef matches (oid = \""+oid+"\")"));
    for(JsonNode c:iter(r.path("object").path("object"))){
      String target=poly(c.path("targetRef").path("targetName"));
      // Skip midPoint's operation/execution wrapper cases (no requested target) — show only real requests.
      if(target.isBlank()) continue;
      String state=c.path("state").asText("");
      String outcome=c.path("outcome").asText("");
      String when=c.path("metadata").path("createTimestamp").asText(c.path("closeTimestamp").asText(""));
      out.add(new RequestStatus(target,state,outcome,when));
    }
    return out;
  }
}
/** Snapshot of an open approval case: requester oid, requested target name, workItemId → approver oids. */
record OpenCase(String caseOid,String requesterOid,String target,java.util.Map<String,List<String>> workItems) {}
record RequestStatus(String target,String state,String outcome,String when) {}
record WorkItemRef(String caseOid,String workItemId) {}
/** Result of resolving a case's actionable work item: status ∈ open|closed|missing, plus the
 *  requested target's resolved name (for labelling the decision receipt). */
record CaseWorkItem(String status,WorkItemRef ref,String targetName) {}
/** A pending approval located just after submit: case + work item + the actual approver emails. */
record PendingApproval(String caseOid,String workItemId,java.util.List<String> approverEmails) {}
record RequestableItem(String type,String midpointType,String name,String oid,String description,String risk) {}
record UserProfile(String oid,String name,String fullName,String email,int assignmentCount,java.util.List<String> roles) {}
record ManagerRef(String oid,String name,String fullName,String email) {}
record CatalogItem(String system,String role,String roleOid,String risk) {}
record CatalogOption(String name,String oid) {}
