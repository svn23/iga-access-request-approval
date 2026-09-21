package in.cnxy.connector;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class WorkItemPollerTest {
    private final MidpointClient mp = mock(MidpointClient.class);
    private final NotifyController notify = mock(NotifyController.class);
    private final WorkItemPoller poller = new WorkItemPoller(mp, notify);

    private static OpenCase open(String wid) {
        return new OpenCase("case1", "u1", "Finance-Admin", Map.of(wid, List.of("mgr1")));
    }

    @Test
    void firstPollOnlyBaselines_thenNewWorkItemRaises_thenClosedCaseCompletes() throws Exception {
        when(mp.emailForOid("u1")).thenReturn("user@x.com");
        when(mp.emailForOid("mgr1")).thenReturn("mgr@x.com");

        when(mp.openApprovalCases()).thenReturn(Map.of("case1", open("1")));
        poller.poll();
        verify(notify, never()).accept(any()); // pre-existing work is baseline, not news

        when(mp.openApprovalCases()).thenReturn(Map.of("case1", open("1"), "case2",
                new OpenCase("case2", "u1", "Role-B", Map.of("7", List.of("mgr1")))));
        poller.poll();
        ArgumentCaptor<NotifyController.NotifyPayload> raised = ArgumentCaptor.forClass(NotifyController.NotifyPayload.class);
        verify(notify).accept(raised.capture());
        assertEquals("ACCESS_REQUEST_RAISED", raised.getValue().event());
        assertEquals("case2", raised.getValue().caseOid());
        assertEquals("mgr@x.com", raised.getValue().managerEmail());
        assertEquals("Role-B", raised.getValue().requestedItem());

        when(mp.openApprovalCases()).thenReturn(Map.of("case1", open("1")));
        when(mp.getCase("case2")).thenReturn(new ObjectMapper().readTree(
                "{\"case\":{\"outcome\":\"http://midpoint.evolveum.com/xml/ns/public/model/approval/outcome#approve\"}}"));
        poller.poll();
        ArgumentCaptor<NotifyController.NotifyPayload> done = ArgumentCaptor.forClass(NotifyController.NotifyPayload.class);
        verify(notify, times(2)).accept(done.capture());
        assertEquals("ACCESS_REQUEST_COMPLETED", done.getValue().event());
        assertEquals("GRANTED", done.getValue().status());
        assertEquals("user@x.com", done.getValue().requesterEmail());
    }

    @Test
    void failedPollKeepsBaseline() {
        when(mp.openApprovalCases()).thenReturn(Map.of("case1", open("1")));
        poller.poll();
        doThrow(new RuntimeException("midPoint down")).when(mp).openApprovalCases();
        poller.poll();
        doReturn(Map.of("case1", open("1"))).when(mp).openApprovalCases();
        poller.poll();
        verify(notify, never()).accept(any());
    }
}
