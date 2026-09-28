import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";

import { IconButton } from "./IconButton";
import styles from "./Modal.module.css";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
}

export function Modal({ open, onOpenChange, title, description, children, footer }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        {/* Без описания Radix требует явный aria-describedby={undefined}. */}
        <Dialog.Content
          className={styles.content}
          {...(description ? {} : { "aria-describedby": undefined })}
        >
          <div className={styles.head}>
            <div>
              <Dialog.Title className={styles.title}>{title}</Dialog.Title>
              {description ? (
                <Dialog.Description className={styles.description}>
                  {description}
                </Dialog.Description>
              ) : null}
            </div>
            <Dialog.Close asChild>
              <IconButton label="Закрыть">
                <X size={20} aria-hidden />
              </IconButton>
            </Dialog.Close>
          </div>
          {children}
          {footer ? <div className={styles.footer}>{footer}</div> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
