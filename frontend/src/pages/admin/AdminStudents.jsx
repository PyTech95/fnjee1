import { useEffect, useState } from "react";
import { usersApi } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import { Trash2, AlertTriangle } from "lucide-react";

export default function AdminStudents() {
  const [role, setRole] = useState("student");
  const [rows, setRows] = useState([]);
  const [confirm1, setConfirm1] = useState(false);
  const [confirm2, setConfirm2] = useState(false);
  const [typed, setTyped] = useState("");
  const [purging, setPurging] = useState(false);
  const { toast } = useToast();

  const load = () => usersApi.list(role).then(setRows);
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [role]);

  const doPurge = async () => {
    setPurging(true);
    try {
      const r = await usersApi.purgeAll();
      toast({ title: "All users deleted", description: `${r.deleted_users} users and ${r.deleted_attempts} attempts removed. Admin accounts kept.` });
      setConfirm2(false); setTyped("");
      load();
    } catch (e) {
      toast({ title: "Delete failed", description: e?.response?.data?.detail || e.message, variant: "destructive" });
    } finally { setPurging(false); }
  };

  return (
    <div data-testid="admin-users-page" className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="text-xs font-bold uppercase tracking-[0.2em] text-primary">People</div>
          <h1 className="font-display font-bold text-3xl tracking-tight mt-1">Users</h1>
        </div>
        <Button data-testid="delete-all-users-btn" variant="destructive" className="rounded-full"
          onClick={() => setConfirm1(true)}>
          <Trash2 className="h-4 w-4 mr-2" /> Delete all users
        </Button>
      </div>

      <Tabs value={role} onValueChange={setRole}>
        <TabsList className="rounded-full">
          <TabsTrigger value="student" className="rounded-full">Students</TabsTrigger>
          <TabsTrigger value="parent" className="rounded-full">Parents</TabsTrigger>
          <TabsTrigger value="admin" className="rounded-full">Admins</TabsTrigger>
        </TabsList>
      </Tabs>

      <Card className="en-card overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>User</TableHead>
              <TableHead>Email</TableHead>
              {role === "student" && <TableHead>Target</TableHead>}
              {role === "student" && <TableHead className="text-right">Coins</TableHead>}
              {role === "student" && <TableHead className="text-right">Streak</TableHead>}
              {role === "parent" && <TableHead>Children</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(u => (
              <TableRow key={u.id} data-testid={`user-row-${u.id}`}>
                <TableCell>
                  <div className="flex items-center gap-3">
                    <Avatar className="h-8 w-8"><AvatarImage src={u.avatar} /><AvatarFallback>{u.name?.[0]}</AvatarFallback></Avatar>
                    <div className="font-medium">{u.name}</div>
                  </div>
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">{u.email}</TableCell>
                {role === "student" && <TableCell><Badge variant="secondary" className="rounded-full">{u.exam_target || "—"}</Badge></TableCell>}
                {role === "student" && <TableCell className="text-right font-medium">{u.reward_coins}</TableCell>}
                {role === "student" && <TableCell className="text-right">{u.streak_days}</TableCell>}
                {role === "parent" && <TableCell>{u.child_ids?.length || 0}</TableCell>}
              </TableRow>
            ))}
            {rows.length === 0 && <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground py-8">No {role}s yet.</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>

      {/* First confirmation */}
      <AlertDialog open={confirm1} onOpenChange={setConfirm1}>
        <AlertDialogContent data-testid="purge-confirm-1">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" /> Delete ALL users?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This removes every student, parent and teacher account and all their attempts
              from the server. Admin accounts are kept. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel data-testid="purge-cancel-1">Cancel</AlertDialogCancel>
            <AlertDialogAction data-testid="purge-continue-1"
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => { setConfirm1(false); setTyped(""); setConfirm2(true); }}>
              Yes, continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Second confirmation — type to confirm */}
      <AlertDialog open={confirm2} onOpenChange={(o) => { setConfirm2(o); if (!o) setTyped(""); }}>
        <AlertDialogContent data-testid="purge-confirm-2">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" /> Final confirmation
            </AlertDialogTitle>
            <AlertDialogDescription>
              Type <span className="font-mono font-bold text-destructive">DELETE ALL USERS</span> below to permanently delete everyone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Input data-testid="purge-confirm-input" value={typed} onChange={(e) => setTyped(e.target.value)}
            placeholder="DELETE ALL USERS" autoFocus />
          <AlertDialogFooter>
            <AlertDialogCancel data-testid="purge-cancel-2">Cancel</AlertDialogCancel>
            <AlertDialogAction data-testid="purge-confirm-final"
              disabled={typed !== "DELETE ALL USERS" || purging}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => { e.preventDefault(); doPurge(); }}>
              {purging ? "Deleting…" : "Delete everyone"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
