import { QueryClient } from "@tanstack/react-query";
import V1ConversationService from "#/api/conversation-service/v1-conversation-service.api";
import { V1AppConversation } from "#/api/conversation-service/v1-conversation-service.types";
import { SandboxService } from "#/api/sandbox-service/sandbox-service.api";
import { V1SandboxStatus } from "#/api/sandbox-service/sandbox-service.types";
import { V1ExecutionStatus } from "#/types/v1/core/base/common";

/**
 * Fetches a V1 conversation's sandbox_id and conversation_url
 */
const fetchV1ConversationData = async (
  conversationId: string,
): Promise<{
  sandboxId: string;
  conversationUrl: string | null;
  sessionApiKey: string | null;
}> => {
  const conversations = await V1ConversationService.batchGetAppConversations([
    conversationId,
  ]);

  const appConversation = conversations[0];
  if (!appConversation) {
    throw new Error(`V1 conversation not found: ${conversationId}`);
  }

  return {
    sandboxId: appConversation.sandbox_id,
    conversationUrl: appConversation.conversation_url,
    sessionApiKey: appConversation.session_api_key,
  };
};

/**
/**
 * "Stop" a V1 conversation from list/menu surfaces.
 *
 * Previously this called ``POST /api/v1/sandboxes/{id}/pause`` which froze
 * the entire sandbox container (including code-server, ssh, and any user
 * services). The new contract uses the app-server's ``/stop`` endpoint,
 * which only interrupts the agent loop and the agent-spawned subprocesses
 * while keeping the sandbox itself running so the user can immediately send
 * a follow-up message without going through a resume flow.
 *
 * The function name and the ``useUnifiedPauseConversationSandbox`` hook
 * name are kept for backwards compatibility with existing call sites.
 */
export const pauseV1ConversationSandbox = async (conversationId: string) => {
  return V1ConversationService.stopConversation(conversationId);
};

/**
 * Pause a V1 conversation by fetching the conversation data and pausing it
 */
export const pauseV1Conversation = async (conversationId: string) => {
  const { conversationUrl, sessionApiKey } =
    await fetchV1ConversationData(conversationId);
  return V1ConversationService.pauseConversation(
    conversationId,
    conversationUrl,
    sessionApiKey,
  );
};

/**
 * Stop a V1 conversation by calling the app-server stop endpoint.
 */
export const stopV1Conversation = async (conversationId: string) => {
  return V1ConversationService.stopConversation(conversationId);
};

/**
 * Ask the agent a side question on a V1 conversation
 */
export const askV1Agent = async (
  conversationId: string,
  question: string,
): Promise<{ response: string }> => {
  const { conversationUrl, sessionApiKey } =
    await fetchV1ConversationData(conversationId);
  return V1ConversationService.askAgent(
    conversationId,
    conversationUrl,
    question,
    sessionApiKey,
  );
};

/**
 * Resumes a V1 conversation sandbox by fetching the sandbox_id and resuming it
 */
export const resumeV1ConversationSandbox = async (conversationId: string) => {
  const { sandboxId } = await fetchV1ConversationData(conversationId);
  return SandboxService.resumeSandbox(sandboxId);
};

/**
 * Resume a V1 conversation by fetching the conversation data and resuming it
 */
export const resumeV1Conversation = async (conversationId: string) => {
  const { conversationUrl, sessionApiKey } =
    await fetchV1ConversationData(conversationId);
  return V1ConversationService.resumeConversation(
    conversationId,
    conversationUrl,
    sessionApiKey,
  );
};

/**
 * Optimistically updates the conversation status in the cache
 */
export const updateConversationSandboxStatusInCache = (
  queryClient: QueryClient,
  conversationId: string,
  sandbox_status: V1SandboxStatus,
): void => {
  // Update the individual conversation cache
  queryClient.setQueryData<V1AppConversation | null>(
    ["user", "conversation", conversationId],
    (oldData) => {
      if (!oldData) return oldData;

      return {
        ...oldData,
        sandbox_status,
        execution_status:
          sandbox_status === "RUNNING" ? oldData.execution_status : null,
      };
    },
  );

  // Update the conversations list cache
  queryClient.setQueriesData<{
    pages: Array<{
      items: Array<{ id: string; sandbox_status: string }>;
    }>;
  }>({ queryKey: ["user", "conversations"] }, (oldData) => {
    if (!oldData) return oldData;

    return {
      ...oldData,
      pages: oldData.pages.map((page) => ({
        ...page,
        items: page.items.map((conv) =>
          conv.id === conversationId ? { ...conv, sandbox_status } : conv,
        ),
      })),
    };
  });
};

/**
 * Update cached V1 conversation execution status.
 */
export const updateConversationExecutionStatusInCache = (
  queryClient: QueryClient,
  conversationId: string,
  execution_status: V1ExecutionStatus | null,
): void => {
  queryClient.setQueryData<V1AppConversation | null>(
    ["user", "conversation", conversationId],
    (oldData) => {
      if (!oldData) return oldData;

      return {
        ...oldData,
        execution_status,
      };
    },
  );

  queryClient.setQueriesData<{
    pages: Array<{
      items: Array<{ id: string; execution_status: V1ExecutionStatus | null }>;
    }>;
  }>({ queryKey: ["user", "conversations"] }, (oldData) => {
    if (!oldData) return oldData;

    return {
      ...oldData,
      pages: oldData.pages.map((page) => ({
        ...page,
        items: page.items.map((conv) =>
          conv.id === conversationId
            ? { ...conv, execution_status }
            : conv,
        ),
      })),
    };
  });
};

/**
 * Invalidates all queries related to conversation mutations (start/stop)
 */
export const invalidateConversationQueries = (
  queryClient: QueryClient,
  conversationId: string,
): void => {
  // Invalidate the specific conversation query to trigger automatic refetch
  queryClient.invalidateQueries({
    queryKey: ["user", "conversation", conversationId],
  });
  // Also invalidate the conversations list for consistency
  queryClient.invalidateQueries({ queryKey: ["user", "conversations"] });
  // Invalidate V1 batch get queries
  queryClient.invalidateQueries({
    queryKey: ["v1-batch-get-app-conversations"],
  });
  // Invalidate sandbox and VS Code URL caches to pick up new runtime URLs after resume
  // Uses partial key matching to invalidate all sandbox-related queries (batch, individual, etc.)
  queryClient.invalidateQueries({ queryKey: ["sandboxes"] });
  queryClient.invalidateQueries({ queryKey: ["unified", "vscode_url"] });
};
