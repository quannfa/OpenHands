import { useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { useTranslation } from "react-i18next";
import { TOAST_OPTIONS } from "#/utils/custom-toast-handlers";
import { I18nKey } from "#/i18n/declaration";
import {
  pauseV1ConversationSandbox,
  updateConversationExecutionStatusInCache,
} from "./conversation-mutation-utils";
import { useV1ConversationStateStore } from "#/stores/v1-conversation-state-store";
import { V1ExecutionStatus } from "#/types/v1/core/base/common";

/**
 * Hook to interrupt a conversation.
 *
 * Despite the legacy "Pause Sandbox" name (kept for backwards compat with
 * existing call sites), this no longer freezes the sandbox container. It
 * sends the request to the app-server ``/stop`` endpoint, which:
 *   1. pauses the agent loop,
 *   2. signals the agent-spawned subprocesses (SIGINT → SIGTERM → SIGKILL),
 *   3. leaves the sandbox container itself running.
 *
 * As a result the user can keep their editor / terminal open and send a
 * follow-up message immediately, without needing a Resume action.
 *
 * Usage:
 * const { mutate: stopConversation } = useUnifiedPauseConversationSandbox();
 * stopConversation({ conversationId: "some-id" });
 */
export const useUnifiedPauseConversationSandbox = () => {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: ["stop-conversation"],
    mutationFn: async (variables: { conversationId: string }) =>
      pauseV1ConversationSandbox(variables.conversationId),
    onMutate: async () => {
      const toastId = toast.loading(
        t(I18nKey.TOAST$STOPPING_CONVERSATION),
        TOAST_OPTIONS,
      );

      await queryClient.cancelQueries({ queryKey: ["user", "conversations"] });
      const previousConversations = queryClient.getQueryData([
        "user",
        "conversations",
      ]);

      return { previousConversations, toastId };
    },
    onError: (_, __, context) => {
      if (context?.toastId) {
        toast.dismiss(context.toastId);
      }
      toast.error(t(I18nKey.TOAST$FAILED_TO_STOP_CONVERSATION), TOAST_OPTIONS);

      if (context?.previousConversations) {
        queryClient.setQueryData(
          ["user", "conversations"],
          context.previousConversations,
        );
      }
    },
    onSuccess: (_, variables, context) => {
      if (context?.toastId) {
        toast.dismiss(context.toastId);
      }
      toast.success(t(I18nKey.TOAST$CONVERSATION_STOPPED), TOAST_OPTIONS);

      // Only the execution status changes — the sandbox keeps running so the
      // user can immediately send a follow-up message without a resume flow.
      useV1ConversationStateStore
        .getState()
        .setExecutionStatus(V1ExecutionStatus.PAUSED);
      updateConversationExecutionStatusInCache(
        queryClient,
        variables.conversationId,
        V1ExecutionStatus.PAUSED,
      );
    },
    onSettled: (_, __, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["user", "conversation", variables.conversationId],
      });
      queryClient.invalidateQueries({ queryKey: ["user", "conversations"] });
      queryClient.invalidateQueries({
        queryKey: ["v1-batch-get-app-conversations"],
      });
    },
  });
};
